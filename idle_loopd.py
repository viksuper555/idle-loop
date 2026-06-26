"""idle-loopd — the resident daemon around the idle-loop orchestrator.

The lifecycle half of epic #20. Where ``idle-listener.sh --watch`` keeps the
board moving with a bash poll loop plus a cron one-shot for rate-limit resets,
this is a clean resident **Python** process that does the same job without the
cron dance and without a Claude session having to stay alive:

* a poll loop that calls :meth:`Orchestrator.run` every ``--interval`` seconds
  and never exits after a pass (each pass already services ``idle:listen`` PR
  reviews *and* works ready tickets — see #25),
* a single-instance PID lock under ``.idle-loop/`` (a second daemon refuses to
  start; a stale lock from a dead PID is reclaimed),
* graceful SIGINT/SIGTERM shutdown (release the lock, exit 0),
* internalised rate-limit recovery — catch :class:`HarnessRateLimited`, sleep
  until the parsed reset epoch, then resume (no cron, no exit 42).

``idle-listener.sh`` is left untouched; this is the additive resident
alternative. Dependency-free: stdlib ``signal``/``os``/``time``/``logging`` only.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from collections.abc import Callable

from agents.harness import HarnessRateLimited

log = logging.getLogger("idle_loopd")

# Default poll cadence; mirrors idle-listener.sh's 600s watch interval.
DEFAULT_INTERVAL = 600
# Single-instance lock, relative to the repo dir (lives beside the other
# .idle-loop/ state the orchestrator already writes).
LOCK_PATH = ".idle-loop/idle-loopd.pid"
# How finely a long sleep is sliced so a stop signal is honoured promptly.
_SLEEP_SLICE_S = 1.0

EXIT_OK = 0
# A live daemon already holds the lock; this instance refused to start.
EXIT_LOCKED = 3


def pid_alive(pid: int) -> bool:
    """Whether process ``pid`` is currently alive (best-effort, POSIX)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user — still alive
    except OSError:
        return False
    return True


class PidLock:
    """A single-instance PID lock file with stale-lock reclamation.

    :meth:`acquire` returns ``True`` and writes our PID when the lock is free or
    held by a dead PID (reclaimed); it returns ``False`` when a *live* process
    already holds it. :meth:`release` removes the file if we hold it.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._held = False

    def _read_pid(self) -> int | None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return int(fh.read().strip())
        except (OSError, ValueError):
            return None

    def acquire(self) -> bool:
        existing = self._read_pid()
        if existing is not None and existing != os.getpid() and pid_alive(existing):
            return False
        # Free, ours, or stale (dead PID) -> take it over.
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        self._held = True
        return True

    def release(self) -> None:
        if not self._held:
            return
        try:
            os.remove(self.path)
        except OSError:
            pass
        self._held = False


class Daemon:
    """Resident poll loop around an :class:`Orchestrator`.

    The orchestrator is injected so tests can supply a fake; time and sleep are
    methods (:meth:`_now`, :meth:`_sleep`) tests can replace to avoid real
    clocks. Construction does not touch the network or the filesystem; side
    effects happen in :meth:`run`.
    """

    def __init__(
        self,
        orchestrator,
        *,
        interval: int = DEFAULT_INTERVAL,
        once: bool = False,
        lock_path: str = LOCK_PATH,
        logger: logging.Logger | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.interval = max(1, interval)
        self.once = once
        self.lock = PidLock(lock_path)
        self.log = logger or log
        self._stop = False

    # ------------------------------------------------------------------ #
    # Signals
    # ------------------------------------------------------------------ #
    def request_stop(self, *_args) -> None:
        """Ask the loop to finish the current wait/pass and exit (signal-safe)."""
        self._stop = True

    def install_signal_handlers(self) -> None:
        """Route SIGINT/SIGTERM to :meth:`request_stop` (main thread only)."""
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

    # ------------------------------------------------------------------ #
    # Time (overridable in tests)
    # ------------------------------------------------------------------ #
    def _now(self) -> float:
        return time.time()

    def _sleep(self, seconds: float) -> None:
        """Sleep ``seconds``, sliced so a stop request is honoured promptly."""
        end = time.monotonic() + max(0.0, seconds)
        while not self._stop:
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(_SLEEP_SLICE_S, remaining))

    # ------------------------------------------------------------------ #
    # Loop
    # ------------------------------------------------------------------ #
    def serve(self) -> int:
        """Install signal handlers, then run the loop. The CLI entrypoint."""
        self.install_signal_handlers()
        return self.run()

    def run(self) -> int:
        """Run the poll loop until stopped (or one pass with ``once``)."""
        if not self.lock.acquire():
            self.log.error(
                "another idle-loopd already holds %s — refusing to start",
                self.lock.path,
            )
            return EXIT_LOCKED
        self.log.info(
            "idle-loopd up (pid %d, interval %ds, once=%s)",
            os.getpid(),
            self.interval,
            self.once,
        )
        try:
            while not self._stop:
                self._one_pass()
                if self.once or self._stop:
                    break
                self._sleep(self.interval)
        finally:
            self.lock.release()
            self.log.info("idle-loopd stopped (lock released)")
        return EXIT_OK

    def _one_pass(self) -> None:
        """One orchestrator pass; rate limit sleeps in place, errors don't kill us."""
        try:
            self.orchestrator.run()
        except HarnessRateLimited as exc:
            self._sleep_until_reset(exc)
        except Exception:  # noqa: BLE001 - one bad pass must not kill the daemon
            self.log.exception("pass crashed; continuing to the next interval")

    def _sleep_until_reset(self, exc: HarnessRateLimited) -> None:
        """Sleep until the harness usage window resets, then return to resume.

        The orchestrator's ``run()`` has already persisted ``.idle-loop/
        rate_limit.json``; we sleep on the exception's ``reset_at`` epoch (the
        same value), falling back to one interval if it is missing/passed.
        """
        reset_at = getattr(exc, "reset_at", None)
        delay = (reset_at - self._now()) if reset_at else float(self.interval)
        delay = max(0.0, delay)
        self.log.warning(
            "rate-limited; sleeping %.0fs until reset, then resuming (no cron)",
            delay,
        )
        self._sleep(delay)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="idle-loopd",
        description="Resident idle-loop daemon: poll the board, service reviews "
        "and work tickets every interval, recover from rate limits in place.",
    )
    parser.add_argument("--config", default="idle.config.yaml", help="path to idle.config.yaml")
    parser.add_argument(
        "--repo-dir",
        default=".",
        help="working directory where the implementer operates on branches",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL,
        help=f"seconds between passes (default {DEFAULT_INTERVAL})",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run exactly one pass and exit (no resident loop)",
    )
    return parser


def _build_daemon(args, orchestrator_factory: Callable | None = None) -> Daemon:
    """Wire a :class:`Daemon` from parsed args (factory injectable for tests)."""
    from config import load_config

    config = load_config(args.config)
    if orchestrator_factory is None:
        from idle_loop import Orchestrator

        orchestrator_factory = Orchestrator.from_config
    orch = orchestrator_factory(config, repo_dir=args.repo_dir)
    return Daemon(
        orch,
        interval=args.interval,
        once=args.once,
        lock_path=os.path.join(args.repo_dir, LOCK_PATH),
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return _build_daemon(args).serve()


if __name__ == "__main__":
    sys.exit(main())
