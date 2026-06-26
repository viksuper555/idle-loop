# idle-loop

> **The guard-railed autonomous coding loop you can actually walk away from.**
> _by Idle._

`idle-loop` watches a GitHub issue board, picks ready tickets one at a time, and runs each
through a full autonomous cycle: **estimate cost → implement on an isolated branch →
fail-closed guardrails → a separate review agent that checks the PR against the ticket's
acceptance criteria → merge only if everything passes, otherwise park it for a human with a
clear reason.**

Its signature, differentiating feature: a **pre-loop cost estimator** that prices each ticket
up front — from a cheap, grounded planning pass (or a zero-token heuristic) — so work is triaged
by ROI: ship the \$30 ticket tonight, flag the \$300 ticket for a human first.

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
./idle-listener.sh -- --max-tickets 3              # one pass; auto-reschedules on rate limit
./idle-listener.sh --watch --interval 900 -- ...   # poll the board every 15 min, forever
./idle-listener.sh --status                        # show pending cron entry / watch mode
./idle-listener.sh --uninstall                     # cancel a pending reschedule + watch state
```

`--watch` keeps polling every `--interval` seconds (default 600). Each pass does **both** halves
of the loop in one go — it services review feedback on open `idle:listen` PRs *and* works new
`idle:ready` tickets (reviews first, so requested changes land before new work spends budget). A
rate limit still parks to cron and resumes the watch when the window resets (watch mode is
persisted across the cron hop). Without `--watch` it's a single backlog pass.

### Review-watching is part of the normal pass

Every `python idle_loop.py` pass (and so every `--watch` interval) checks open `idle:listen` PRs —
the ones idle-loop opened — for new **reviews** and revises the branch in place to address them, up
to `budget.review_iterations` times before handing the PR to a human. No separate command is
required; `--watch-reviews` remains only as an explicit review-only entrypoint for back-compat.

> **What counts as a review.** This responds to PR **reviews** — a *Request changes*, or a
> *Comment* review submitted **with a body** (GitHub's review flow, "Files changed" → *Review
> changes*). It does **not** see plain conversation comments typed in the PR's main comment box
> (those are issue comments, a different API). Leave feedback as a review for idle-loop to act on it.

Set `IDLE_PYTHON` if `python3` isn't your interpreter. On macOS the cron daemon may need Full
Disk Access (System Settings → Privacy & Security), and `claude` must be on `PATH` (the listener
bakes the resolved `PATH` into the cron entry).

#### `idle_loopd.py` — the resident daemon (alternative to the bash listener)

`idle_loopd.py` is a clean, stdlib-only resident **Python** process that does the same job as
`idle-listener.sh --watch` without the cron one-shot dance and without a Claude session having to
stay alive. It calls `Orchestrator.run()` every interval (each pass services `idle:listen` reviews
*and* works tickets), and never exits after a pass.

```bash
python idle_loopd.py                 # resident loop, 600s interval (Ctrl-C to stop)
python idle_loopd.py --interval 300  # poll every 5 min
python idle_loopd.py --once          # a single pass, then exit
```

- **Single-instance.** A PID lock at `.idle-loop/idle-loopd.pid` makes a second daemon refuse to
  start; a lock left by a dead PID is reclaimed automatically.
- **Graceful stop.** SIGINT/SIGTERM finishes the current wait and exits 0, releasing the lock.
- **Rate-limit recovery is internal.** A `HarnessRateLimited` from a pass is caught and the daemon
  sleeps until the parsed reset epoch (the same `.idle-loop/rate_limit.json` the listener reads),
  then resumes — no cron, no exit 42.

`idle-listener.sh` is left in place for back-compat; pick whichever supervisor you prefer.

**From a Claude Code session**, the `/idle-loop` skill (`.claude/skills/idle-loop/`) is the manual
front door — it walks dry-run estimates, label sync, and a bounded pass. The unattended daemon,
though, is the listener + cron above (a skill only runs inside a session).

### Per-agent GitHub identities

By default every comment idle-loop posts uses one token, so the planner, reviewer, implementer, and
the loop itself all show up as the **same** GitHub user — confusing on a thread. Opt into distinct
identities so each agent comments under its own username:

1. **Create one GitHub App per agent, owned by `viksuper555`** (browser — `gh` can't register apps).
   For each of `planner`, `implementer`, `reviewer`, `loop`:
   - **Settings → Developer settings → GitHub Apps → New GitHub App** (under the `viksuper555` account).
   - Name it distinctly (`idle-planner`, `idle-implementer`, …) — that name becomes the `…[bot]` comment
     author. Set any Homepage URL; under **Webhook**, uncheck **Active**.
   - **Permissions → Repository:** Issues **R&W**, Pull requests **R&W**, Contents **R&W** (the
     implementer commits/pushes), Metadata **R**.
   - Create it, **Generate a private key** (downloads a `.pem`), and note the **App ID**.
   - **Install App** on `viksuper555/idle-loop` (only-select-repositories).
2. **Collect credentials** into `.idle-loop/agents-apps.json` (gitignored — copy
   `examples/agents-apps.example.json`); drop each `.pem` under `.idle-loop/keys/`. Each
   `installation_id` is in the install URL (`.../installations/<id>`) or via
   `gh api "/repos/viksuper555/idle-loop/installation" --jq .id`.
3. **Mint tokens and run** — installation tokens last ~1h, so re-mint per session:

   ```bash
   ./mint-agent-tokens.sh            # -> .idle-loop/agents.env (gitignored, chmod 600)
   ./mint-agent-tokens.sh --check    # validate config + report each bot login, no write
   source .idle-loop/agents.env      # export IDLE_GH_TOKEN_* for this shell
   python idle_loop.py --max-tickets N
   ```

   `identities.enabled: true` and the `tokens:` env-var map are already set in `idle.config.yaml`.
   **Tokens never live in the yaml or in git** — only the env-var *names* do.

Any agent whose env var is unset falls back to the shared default token, so this is safe to enable
incrementally. With it off (the default) behaviour is exactly as before. The cost chip posts as the
**planner**, the review verdict as the **reviewer**, in-PR revision notes as the **implementer**,
and parks/merges/guard verdicts as the **loop**.

### CLI

| Flag | Default | Meaning |
|------|---------|---------|
| `--config PATH` | `idle.config.yaml` | Config file to load. |
| `--dry-run` | off | List ready issues + their cost band; take **no** action. |
| `--repo-dir DIR` | `.` | Working directory where the implementer operates on branches. |
| `--max-tickets N` | unlimited | Process at most N tickets this run. |
| `--watch-reviews` | off | Review-only pass: address new reviews on open `idle:listen` PRs, then exit. The normal pass already does this, so it's optional. |

---

## The signature feature — pre-loop cost estimation

> **Provenance:** the pre-loop, per-ticket cost-estimation gate (`guards/estimate.py`) is this
> project's original contribution. Please keep this attribution.

idle-loop prices each ticket one of two ways. The **heuristic** `Estimator` spends *zero tokens* —
it scores each ticket on cheap, explainable proxies and prices it:

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

**Deterministic mode (default).** Instead of inferring cost from iteration counts, the loop
prices a ticket from a real, grounded token budget: a cheap **planning pass** (`agents/planner.py`)
runs a read-only `claude` session that inspects the actual code and reports the input/output
tokens the work will take, and `estimated_cost = pricing.cost_for_tokens(...)`. The implementer
then **resumes that same session**, so the plan carries straight into implementation. This spends
a small, bounded planning pass up front (so estimation is no longer strictly zero-token); a cheap
heuristic pre-filter still parks obviously-over-budget tickets without planning, and `--dry-run`
plus any planning failure fall back to the zero-token heuristic above. Set `planner.enabled: false`
to always use the heuristic. The issue's cost chip then carries a sibling **tokens** badge —
tokens spent (from the session's own usage) vs the planning estimate.

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
