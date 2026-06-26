#!/usr/bin/env bash
#
# idle-listener — a local supervisor for the idle-loop Claude Code agent.
#
# Runs one idle-loop session (python idle_loop.py). The agents go through the
# Claude Code harness (the `claude` CLI), so auth is your Claude login — no API
# key. When the harness hits a usage/session limit, idle_loop.py exits 42 and
# writes the reset time to .idle-loop/rate_limit.json. This listener reads that,
# installs a SELF-REMOVING cron one-shot at the reset time to start a fresh
# session, and exits. Each cron-triggered run clears its own entry first, so the
# schedule never stacks up.
#
# Usage:
#   ./idle-listener.sh [-- <args passed to idle_loop.py>]
#   ./idle-listener.sh --uninstall        # remove any pending cron entry
#   ./idle-listener.sh --status           # show pending cron entry, if any
#   ./idle-listener.sh --from-cron [...]  # internal: invoked by cron
#
# Example: ./idle-listener.sh -- --max-tickets 3
#
# macOS note: the cron daemon may need Full Disk Access (System Settings ->
# Privacy & Security) to run, and `claude` must be resolvable on PATH (this
# script bakes the resolved PATH into the cron entry).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$REPO_DIR/$(basename "${BASH_SOURCE[0]}")"
cd "$REPO_DIR"

STATE_DIR="$REPO_DIR/.idle-loop"
LOG="$STATE_DIR/listener.log"
STATE="$STATE_DIR/rate_limit.json"
ARGS_FILE="$STATE_DIR/loop_args"
CRON_TAG="# idle-loop-listener"
RATE_LIMITED_EXIT=42
FALLBACK_WINDOW_S=18000   # 5h, if no reset time is available

mkdir -p "$STATE_DIR"

PY="${IDLE_PYTHON:-$(command -v python3 || command -v python || true)}"
CLAUDE_BIN="$(command -v claude || true)"

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG" >&2; }

usage() { sed -n '3,30p' "$SCRIPT" | sed 's/^# \{0,1\}//'; }

# --- crontab helpers ------------------------------------------------------- #
remove_cron() {
  local current
  current="$(crontab -l 2>/dev/null || true)"
  if printf '%s\n' "$current" | grep -qF "$CRON_TAG"; then
    printf '%s\n' "$current" | grep -vF "$CRON_TAG" | crontab - || true
    log "cleared pending cron entry"
  fi
}

epoch_to_hm() {  # $1 epoch -> prints "MIN HOUR"; supports BSD (macOS) + GNU date
  local epoch="$1"
  if date -r "$epoch" +%M >/dev/null 2>&1; then
    date -r "$epoch" '+%M %H'
  else
    date -d "@$epoch" '+%M %H'
  fi
}

epoch_human() {
  local epoch="$1"
  if date -r "$epoch" >/dev/null 2>&1; then date -r "$epoch"; else date -d "@$epoch"; fi
}

schedule_cron() {  # $1 = reset epoch
  local epoch="$1" min hour pathspec line current
  read -r min hour < <(epoch_to_hm "$epoch")
  pathspec="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
  [ -n "$CLAUDE_BIN" ] && pathspec="$(dirname "$CLAUDE_BIN"):$pathspec"
  [ -n "$PY" ] && pathspec="$(dirname "$PY"):$pathspec"
  # Daily HH:MM. The run self-removes the entry, so it fires once at the next
  # occurrence of that time (i.e. the reset) and then disappears.
  line="$min $hour * * * cd $REPO_DIR && PATH=$pathspec $SCRIPT --from-cron >> $LOG 2>&1 $CRON_TAG"
  current="$(crontab -l 2>/dev/null || true)"
  { printf '%s\n' "$current" | grep -vF "$CRON_TAG"; printf '%s\n' "$line"; } | crontab -
  log "scheduled next session at ${hour}:${min} (≈ $(epoch_human "$epoch")) via cron"
}

reset_epoch() {  # echo the reset epoch from state, else a fallback
  local epoch=""
  if [ -f "$STATE" ] && [ -n "$PY" ]; then
    epoch="$("$PY" -c 'import json,sys;print(int(json.load(open(sys.argv[1]))["reset_epoch"]))' "$STATE" 2>/dev/null || true)"
  fi
  if [ -z "$epoch" ]; then
    epoch=$(( $(date +%s) + FALLBACK_WINDOW_S ))
    log "no parseable reset time; falling back to +5h"
  fi
  printf '%s\n' "$epoch"
}

# --- argument parsing ------------------------------------------------------ #
FROM_CRON=0
LOOP_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --from-cron) FROM_CRON=1; shift ;;
    --uninstall) remove_cron; log "uninstalled"; exit 0 ;;
    --status)
      crontab -l 2>/dev/null | grep -F "$CRON_TAG" || echo "no pending idle-loop cron entry"
      exit 0 ;;
    -h|--help) usage; exit 0 ;;
    --) shift; LOOP_ARGS=("$@"); break ;;
    *) log "unknown argument: $1 (use -- to pass args to idle_loop.py)"; exit 2 ;;
  esac
done

# A cron-triggered run clears the entry that fired (one-shot semantics) and
# restores the loop args saved when it was scheduled.
if [ "$FROM_CRON" -eq 1 ]; then
  remove_cron
  if [ ${#LOOP_ARGS[@]} -eq 0 ] && [ -f "$ARGS_FILE" ]; then
    mapfile -t LOOP_ARGS < "$ARGS_FILE" 2>/dev/null || true
  fi
fi

# Persist loop args so a rescheduled run reuses them.
: > "$ARGS_FILE"
for a in "${LOOP_ARGS[@]:-}"; do [ -n "$a" ] && printf '%s\n' "$a" >> "$ARGS_FILE"; done

if [ -z "$PY" ]; then log "no python found (set IDLE_PYTHON)"; exit 127; fi
if [ -z "$CLAUDE_BIN" ]; then log "WARNING: 'claude' not on PATH — the agents will fail"; fi

# --- run one session ------------------------------------------------------- #
log "starting idle-loop session (args: ${LOOP_ARGS[*]:-none})"
set +e
"$PY" idle_loop.py "${LOOP_ARGS[@]:-}" >> "$LOG" 2>&1
rc=$?
set -e

if [ "$rc" -eq "$RATE_LIMITED_EXIT" ]; then
  log "rate-limited (exit $rc) — rescheduling"
  schedule_cron "$(reset_epoch)"
  exit 0
fi

log "session ended (exit $rc); nothing rescheduled"
exit "$rc"
