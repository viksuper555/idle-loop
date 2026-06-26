"""Config loading for idle-loop.

Mirrors ``idle.config.yaml`` (see §9 of the spec) onto typed dataclasses with
sane defaults and light validation. stdlib + PyYAML only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = "idle.config.yaml"


@dataclass
class Labels:
    ready: str = "idle:ready"
    needs_human: str = "idle:needs-human"
    allow_sensitive: str = "idle:allow-sensitive"
    # Opt-in budget override: a ticket carrying this label may exceed the default
    # per-ticket caps up to the configured override ceilings (Budget.override_*),
    # and is re-dispatched even when parked. The global cap still applies.
    allow_budget: str = "idle:allow-budget"
    # Applied to a PR the moment idle-loop opens it; idle keeps watching the PR
    # for new reviews while it carries this label, and drops it once the PR is
    # deferred to a human (see Orchestrator.watch_reviews).
    listen: str = "idle:listen"
    # Applied to an issue the moment idle-loop opens a PR for it; discover() skips
    # tickets carrying it so a ticket with an open idle-loop PR is not re-worked
    # from scratch. Cleared when the PR merges/closes (see _reap_worktrees).
    in_progress: str = "idle:in-progress"


@dataclass
class Triage:
    auto_threshold_usd: float = 50.0
    # Heuristic pre-filter: skip the (token-spending) planning pass on tickets the
    # cheap heuristic already prices above auto_threshold_usd * this multiplier.
    prefilter_multiplier: float = 2.0


@dataclass
class Budget:
    max_iterations: int = 20  # per ticket
    per_ticket_cap_usd: float = 40.0
    global_cap_usd: float = 200.0
    # Elevated per-ticket ceilings used only for a ticket carrying the
    # labels.allow_budget override; the global cap is never raised.
    override_max_iterations: int = 40
    override_per_ticket_cap_usd: float = 80.0
    no_progress_limit: int = 3  # identical error / empty diff N times -> bail
    review_iterations: int = 2  # times to send reviewer feedback back to the
    # implementer on the SAME branch before parking for a human
    max_parallel: int = 3  # tickets worked concurrently, each in its own git
    # worktree (1 = sequential). Concurrent runs share the same usage window.


@dataclass
class Guards:
    max_diff_lines: int = 4000
    max_files: int = 15
    path_allowlist: list[str] = field(default_factory=lambda: ["src/**", "tests/**"])
    path_denylist: list[str] = field(
        default_factory=lambda: [
            "**/secrets/**",
            ".github/**",
            "infra/**",
            "**/migrations/**",
        ]
    )
    require_tests_for_new_behavior: bool = True


@dataclass
class Merge:
    require_human: bool = True  # default ON for protected branches
    target_branch: str = "main"


@dataclass
class Pricing:
    """USD per 1M tokens for the coding model — used to convert tokens to cost
    and to seed the estimator's measured-cost-per-iteration prior."""

    input_per_mtok: float = 5.0  # claude-opus-4-8 input
    output_per_mtok: float = 25.0  # claude-opus-4-8 output
    # Fallback prior for estimated_cost when the cost log has no data yet.
    default_cost_per_iteration_usd: float = 2.0

    def cost_for_tokens(self, input_tokens: float, output_tokens: float) -> float:
        """USD for a token budget at the configured per-MTok rates."""
        return (
            input_tokens / 1_000_000.0 * self.input_per_mtok
            + output_tokens / 1_000_000.0 * self.output_per_mtok
        )


@dataclass
class Estimator:
    use_learned: bool = False  # toggle the v2 regression once enough rows exist
    min_rows_for_learned: int = 20


@dataclass
class Planner:
    """The deterministic planning pass that prices a ticket via a real claude
    session (a token budget), replacing the iteration-based heuristic.

    Disabling it falls back to the heuristic estimator everywhere.
    """

    enabled: bool = True
    model: str | None = None  # None -> inherit Config.model
    timeout_s: int = 600  # planning is short; don't inherit the implementer's wall
    margin_fraction: float = 0.25  # band width as a fraction of the deterministic cost


@dataclass
class Harness:
    """How the agents reach Claude.

    The default backend is the **Claude Code harness** (the ``claude`` CLI in
    headless ``-p`` mode), which authenticates via your Claude login — no API
    key. The implementer hands the whole ticket to Claude Code and lets it edit
    files / run tests with its own tools; the reviewer runs read-only.
    """

    backend: str = "claude_code"  # "claude_code" (only backend; here for forward-compat)
    claude_bin: str = "claude"  # binary name or absolute path
    skip_permissions: bool = True  # --dangerously-skip-permissions (unattended)
    output_format: str = "json"
    extra_args: list[str] = field(default_factory=list)
    timeout_s: int = 3600  # hard wall-clock per claude invocation
    # When the harness reports a usage/session limit with no parseable reset
    # time, assume the window resets this many hours out.
    rate_limit_window_hours: float = 5.0


@dataclass
class Identities:
    """Per-agent GitHub identities, so each agent comments under its own name.

    ``enabled`` off (default) -> every agent shares the default token, i.e.
    current behaviour. ``tokens`` maps each logical agent to the env var holding
    its GitHub App installation token (or a bot PAT); a maintainer provisions
    those out of band (secrets). An agent whose env var is unset falls back to
    the shared default.
    """

    enabled: bool = False
    tokens: dict[str, str] = field(
        default_factory=lambda: {
            "planner": "IDLE_GH_TOKEN_PLANNER",
            "implementer": "IDLE_GH_TOKEN_IMPLEMENTER",
            "reviewer": "IDLE_GH_TOKEN_REVIEWER",
            "loop": "IDLE_GH_TOKEN_LOOP",
        }
    )


@dataclass
class Config:
    repo: str = ""
    model: str = "claude-opus-4-8"
    cost_log_path: str = "cost_log.jsonl"
    # Working-memory mode for the implementer between turns/runs:
    #   False (default) -> RESUME: continue the prior claude session via --resume.
    #       Fast (warm context) but machine-local — the transcript can't move
    #       between machines, so cross-PC handoff is impossible.
    #   True -> PROGRESS: author and commit a portable PROGRESS.md and feed it into
    #       every prompt INSTEAD of resuming, so a cold start on any machine can
    #       continue from committed git state alone. No --resume in this mode.
    progress_memory: bool = False
    labels: Labels = field(default_factory=Labels)
    triage: Triage = field(default_factory=Triage)
    budget: Budget = field(default_factory=Budget)
    guards: Guards = field(default_factory=Guards)
    merge: Merge = field(default_factory=Merge)
    pricing: Pricing = field(default_factory=Pricing)
    estimator: Estimator = field(default_factory=Estimator)
    planner: Planner = field(default_factory=Planner)
    harness: Harness = field(default_factory=Harness)
    identities: Identities = field(default_factory=Identities)

    def validate(self) -> None:
        if not self.repo or "/" not in self.repo:
            raise ValueError(
                f"config.repo must be 'owner/name', got {self.repo!r}"
            )
        if self.budget.per_ticket_cap_usd > self.budget.global_cap_usd:
            raise ValueError("budget.per_ticket_cap_usd exceeds global_cap_usd")


def _build(cls: type, data: dict[str, Any]) -> Any:
    """Construct a dataclass from a dict, ignoring unknown keys."""
    known = {f.name for f in fields(cls)}
    kwargs = {k: v for k, v in data.items() if k in known}
    return cls(**kwargs)


def load_config(path: str = DEFAULT_CONFIG_PATH) -> Config:
    """Load and validate config from a YAML file.

    Unknown keys are ignored; missing keys fall back to defaults. The
    ``triage.auto_threshold_usd`` key is also accepted under the legacy spelling
    used in the spec (``triage.auto_threshold``).
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"config file not found: {path}")
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    cfg = Config(
        repo=raw.get("repo", ""),
        model=raw.get("model", Config.model),
        cost_log_path=raw.get("cost_log_path", Config.cost_log_path),
        progress_memory=bool(raw.get("progress_memory", Config.progress_memory)),
    )
    if isinstance(raw.get("labels"), dict):
        cfg.labels = _build(Labels, raw["labels"])
    if isinstance(raw.get("triage"), dict):
        triage = dict(raw["triage"])
        if "auto_threshold" in triage and "auto_threshold_usd" not in triage:
            triage["auto_threshold_usd"] = triage["auto_threshold"]
        cfg.triage = _build(Triage, triage)
    if isinstance(raw.get("budget"), dict):
        cfg.budget = _build(Budget, raw["budget"])
    if isinstance(raw.get("guards"), dict):
        cfg.guards = _build(Guards, raw["guards"])
    if isinstance(raw.get("merge"), dict):
        cfg.merge = _build(Merge, raw["merge"])
    if isinstance(raw.get("pricing"), dict):
        cfg.pricing = _build(Pricing, raw["pricing"])
    if isinstance(raw.get("estimator"), dict):
        cfg.estimator = _build(Estimator, raw["estimator"])
    if isinstance(raw.get("planner"), dict):
        cfg.planner = _build(Planner, raw["planner"])
    if isinstance(raw.get("harness"), dict):
        cfg.harness = _build(Harness, raw["harness"])
    if isinstance(raw.get("identities"), dict):
        cfg.identities = _build(Identities, raw["identities"])

    cfg.validate()
    return cfg
