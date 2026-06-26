# idle-loop

> **The guard-railed autonomous coding loop you can actually walk away from.**
> _by Idle._

`idle-loop` watches a GitHub issue board, picks ready tickets one at a time, and runs each
through a full autonomous cycle: **estimate cost → implement on an isolated branch →
fail-closed guardrails → a separate review agent that checks the PR against the ticket's
acceptance criteria → merge only if everything passes, otherwise park it for a human with a
clear reason.**

Its signature, differentiating feature: a **pre-loop cost estimator** that prices each ticket
*before any tokens are spent*, so work is triaged by ROI — ship the \$30 ticket tonight, flag
the \$300 ticket for a human first.

---

## Why

Naive autonomous loops ("loopmaxxing") burn budget because they run without verifiable exit
conditions or feedback gates. The durable, valuable part of the loop-engineering trend is the
**guardrails**. `idle-loop` ships the guardrails — plus the one thing nobody else does:
**upfront per-ticket cost estimation**.

---

## The loop

One iteration = one ticket.

```
        ┌──────────────────────────────────────────────────────────────────────┐
        │                                                                        │
   ┌────▼─────┐   ┌──────────┐   ┌─────────┐   ┌───────────┐   ┌────────────┐    │
   │ DISCOVER │──▶│ ESTIMATE │──▶│ TRIAGE  │──▶│ IMPLEMENT │──▶│   GUARD    │    │
   │ idle:    │   │ price the│   │ > cap?  │   │ agent on  │   │ scope·tests│    │
   │ ready    │   │ ticket   │   │ park it │   │ a branch  │   │ security·  │    │
   └──────────┘   │ ~$X ± $Y │   └────┬────┘   └───────────┘   │ budget     │    │
        ▲         └──────────┘  proceed│                       └─────┬──────┘    │
        │                              │                       fail  │ pass      │
        │  none ready → exit           ▼                       ┌──────▼──────┐    │
        │  (verifiable stop)        skip & label               │  OPEN PR    │    │
        │                           idle:needs-human           └──────┬──────┘    │
        │                                                             ▼           │
   ┌────┴─────┐   ┌──────────────────────┐   ┌────────────────┐  ┌────────┐      │
   │   LOG    │◀──│       DECIDE          │◀──│     REVIEW     │◀─│ (diff) │      │
   │ cost_log │   │ guards green +        │   │ separate agent │  └────────┘      │
   │ .jsonl   │   │ approve + (human?) →  │   │ vs acceptance  │                  │
   └──────────┘   │ merge, else park      │   │ criteria       │──────────────────┘
                  └──────────────────────┘   └────────────────┘
```

**Every gate fails closed.** Nothing merges without passing *all* guards *and* review-agent
approval (and a human, if configured).

---

## Quickstart

```bash
# 1. Install (Python 3.11+)
pip install -e ".[dev]"

# 2. Configure your target repo + thresholds
$EDITOR idle.config.yaml          # set repo: "owner/name"

# 3. Auth — NO API key. The agents run through the Claude Code harness, so just
#    log in once:  claude  (then /login)   — verify with `claude --version`.
#    The GitHub client uses GITHUB_TOKEN / GH_TOKEN (or `gh auth token`).
export GITHUB_TOKEN=ghp_...        # or: gh auth login

# 4. Dry run — list ready issues with a cost estimate each, taking NO action
#    (estimator + GitHub only; never invokes the harness)
python idle_loop.py --dry-run

# 5. Real run — process affordable tickets (parks the rest)
python idle_loop.py --max-tickets 3

# …or run it unattended, surviving rate limits (see "Unattended" below):
./idle-listener.sh -- --max-tickets 3
```

A ticket is **ready** when it carries the `idle:ready` label **and** has an
`## Acceptance Criteria` checklist — tickets without acceptance criteria are rejected, never
guessed at.

### How the agents run — the Claude Code harness (no API key)

Both agents go through the **`claude` CLI** in headless `-p` mode, authenticated by your
Claude login — there is **no `ANTHROPIC_API_KEY`**.

- **Implementer** hands the whole ticket to `claude -p` *inside the target repo on an isolated
  branch* and lets Claude Code do the work with its own tools (edit, bash, run tests). It runs
  with `--dangerously-skip-permissions` so unattended runs don't hang on a prompt — safe here
  because the work lands on a throwaway branch and the guards + reviewer gate the diff before
  anything merges. idle-loop reads the resulting diff and the harness-reported cost back out.
- **Reviewer** runs `claude -p` read-only (mutating tools disallowed, in a temp dir) and returns
  a structured JSON verdict against the acceptance criteria.

Configure under `harness:` in `idle.config.yaml` (`claude_bin`, `skip_permissions`, `timeout_s`,
`rate_limit_window_hours`, `extra_args`).

### Unattended — the listener + cron (survives rate limits)

`idle-listener.sh` supervises a run locally. When the harness hits a usage/session limit,
`idle_loop.py` exits `42` and writes the reset time to `.idle-loop/rate_limit.json`; the listener
reads it and installs a **self-removing cron one-shot** at the reset time to start a fresh
session — then exits. Each cron-triggered run clears its own entry first, so the schedule never
stacks.

```bash
./idle-listener.sh -- --max-tickets 3   # run; auto-reschedules on rate limit
./idle-listener.sh --status             # show the pending cron entry, if any
./idle-listener.sh --uninstall          # cancel a pending reschedule
```

Set `IDLE_PYTHON` if `python3` isn't your interpreter. On macOS the cron daemon may need Full
Disk Access (System Settings → Privacy & Security), and `claude` must be on `PATH` (the listener
bakes the resolved `PATH` into the cron entry).

### CLI

| Flag | Default | Meaning |
|------|---------|---------|
| `--config PATH` | `idle.config.yaml` | Config file to load. |
| `--dry-run` | off | List ready issues + their cost band; take **no** action. |
| `--repo-dir DIR` | `.` | Working directory where the implementer operates on branches. |
| `--max-tickets N` | unlimited | Process at most N tickets this run. |

---

## The signature feature — pre-loop cost estimation

> **Provenance:** the pre-loop, per-ticket cost-estimation gate (`guards/estimate.py`) is this
> project's original contribution. Please keep this attribution.

Before a single token is spent, the `Estimator` scores each ticket on cheap, explainable
proxies and prices it:

- **number of acceptance criteria**,
- **estimated files/modules touched** (keyword/path match of the ticket text against the repo tree),
- **requires-new-tests** flag,
- **ambiguity penalty** (vague spec → more iterations → higher cost),
- **similarity to past tickets** (reuse their *measured* cost when close).

These combine into `estimated_iterations`, then
`estimated_cost = estimated_iterations × measured_avg_cost_per_iteration(this repo, model)`.

The result is rendered honestly as a **band** — `~$30 ± $20` — never false precision. Tickets
priced above `triage.auto_threshold_usd` are parked for a human *before* the loop runs them,
with the estimate commented on the issue.

**It gets better the more it runs.** Every processed ticket logs its pre-estimate features
alongside the *actual* cost and iterations (see below). Flip `estimator.use_learned: true` and,
once enough rows exist, a small regression over those rows is fit and swapped in behind the
exact same interface (optional `scikit-learn`; degrades silently to the heuristic if absent).

---

## Guards (all fail closed)

| Guard | File | Enforces |
|-------|------|----------|
| **scope** | `guards/scope.py` | Max diff size & file count; a path allowlist; hard-denies edits to secrets, CI config, infra/IaC, and DB migrations unless the ticket carries `idle:allow-sensitive`. |
| **tests** | `guards/tests.py` | The suite must be green; new behavior with no accompanying test fails. |
| **security** | `guards/security.py` | Scans the diff for leaked secrets/keys and obvious injection patterns (defensive only). |
| **budget** | `guards/budget.py` | Per-ticket iteration & dollar caps, plus a no-progress detector (same error signature or empty diff *N* times → bail). |
| **estimate** | `guards/estimate.py` | The pre-loop cost gate (above). |

A guard never lets a doubtful change through: any exception inside a guard is treated as a
failure (`guards/base.py:run_guard`).

---

## Cost log — `cost_log.jsonl`

One JSON object per processed ticket — both the audit trail and the training set for the
learned estimator:

```json
{ "ticket_id": 7, "title": "Add retry to fetch",
  "features": { "n_criteria": 3, "est_files": 2, "needs_tests": true, "ambiguity_score": 0.1 },
  "estimated_cost": 14.8, "estimated_iterations": 7.4,
  "actual_cost": 11.20, "actual_iterations": 6,
  "outcome": "merged", "timestamp": "2026-06-26T06:00:00+00:00" }
```

---

## Config reference (`idle.config.yaml`)

| Key | Default | Meaning |
|-----|---------|---------|
| `repo` | — | Target repository, `owner/name` (required). |
| `model` | `claude-opus-4-8` | Coding-agent model. |
| `cost_log_path` | `cost_log.jsonl` | Where run records are appended. |
| `labels.ready` / `.needs_human` / `.allow_sensitive` | `idle:ready` / `idle:needs-human` / `idle:allow-sensitive` | Board labels. |
| `triage.auto_threshold_usd` | `50` | Above this estimate, park for a human pre-run. |
| `budget.max_iterations` | `20` | Per-ticket agent iteration cap. |
| `budget.per_ticket_cap_usd` | `40` | Per-ticket dollar cap. |
| `budget.global_cap_usd` | `200` | Stop the whole loop here. |
| `budget.no_progress_limit` | `3` | Same error / empty diff N times → bail. |
| `guards.max_diff_lines` / `.max_files` | `400` / `15` | Scope limits. |
| `guards.path_allowlist` / `.path_denylist` | `src/**,tests/**` / secrets, `.github/**`, infra, migrations | Scope paths. |
| `merge.require_human` | `true` | Require human approval before merge (default ON for protected branches). |
| `merge.target_branch` | `main` | PR base branch. |
| `estimator.use_learned` | `false` | Swap in the learned (v2) estimator once enough rows exist. |
| `harness.claude_bin` | `claude` | Claude Code CLI binary (name or absolute path). |
| `harness.skip_permissions` | `true` | Pass `--dangerously-skip-permissions` (unattended). |
| `harness.timeout_s` | `3600` | Hard wall-clock per `claude` invocation. |
| `harness.rate_limit_window_hours` | `5.0` | Fallback reset window if the limit notice can't be parsed. |

---

## Safety / fail-closed

- Every gate fails closed; an error in a guard counts as a failure.
- `merge.require_human` is **on by default** — `idle-loop` opens a PR and parks it for human
  approval rather than auto-merging to a protected branch.
- The implementer confines file edits to the repo working directory (path-traversal is
  rejected) and stays inside the scope allowlist; sensitive paths require an explicit label.
- Hard budget caps (per-ticket and global) bound spend; the no-progress detector stops thrash.

---

## Development

```bash
pip install -e ".[dev]"
ruff check .
python -m pytest -q
# optional v2 estimator deps:
pip install -e ".[learned]"
```

A flat-layout project (top-level modules + `agents/` and `guards/` packages). Tests mock every
external boundary — no network, no Anthropic calls, no real git — so the suite runs offline.

See [`examples/`](examples/) for a tiny target repo and seed issues to run the loop end-to-end.

---

<sub>🤖 Shipped by [idle-loop](https://github.com/viksuper555/idle-loop).</sub>
