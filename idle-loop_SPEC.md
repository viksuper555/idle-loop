# idle-loop — Build Spec

> **Tagline:** the guard-railed autonomous coding loop you can actually walk away from.
> **By:** Idle.
> **Audience for this doc:** AI coding agents (and humans) building the project. Read it as the source of truth; if something here is ambiguous, leave a `TODO(spec)` note and a question rather than guessing.

---

## 1. What this is

`idle-loop` watches a GitHub issue board, picks ready tickets one at a time, and runs each through a full autonomous cycle: estimate cost → implement on an isolated branch → fail-closed guardrails → a **separate review agent** that checks the PR against the ticket's acceptance criteria → merge only if everything passes, otherwise park it for a human with a clear reason.

Its signature, differentiating feature: a **pre-loop cost estimator** that prices each ticket *before any tokens are spent*, so work is triaged by ROI (ship the $30 ticket tonight; flag the $300 ticket for a human first).

## 2. Why (positioning context — build agents may skip)

Naive autonomous loops ("loopmaxxing") burn budget because they run without verifiable exit conditions or feedback gates. The durable, valuable part of the loop-engineering trend is the **guardrails**. `idle-loop` ships the guardrails — plus the one thing nobody else does: **upfront per-ticket cost estimation**.

## 3. Goals

- **Autonomous:** process a backlog of well-specified tickets end-to-end with no human in the inner loop.
- **Safe by default:** every gate fails closed. Nothing merges without passing *all* guards *and* review-agent approval.
- **Cost-predictable:** estimate per-ticket cost before running; enforce hard budget caps during.
- **Transparent:** every action logged; every parked PR carries a human-readable reason.
- **Self-improving:** cost estimates get more accurate as real run data accumulates.

## 4. Non-goals (scope guardrails — do NOT build these in v1)

- No web UI or dashboard. CLI + GitHub PRs only.
- No multi-repo orchestration. One target repo per run.
- No model fine-tuning or training.
- No handling of tickets that lack acceptance criteria — reject them, don't guess.
- No auto-merge to protected branches without human approval (configurable; default **off**).

## 5. Architecture — the loop

One iteration = one ticket.

1. **Discover** — fetch open issues labeled `idle:ready`, sorted by priority. None → exit cleanly (this is the verifiable stop condition).
2. **Estimate** — `estimator` prices the ticket (see §7). Output: estimated cost + confidence band.
3. **Triage** — if estimated cost > `triage.auto_threshold`, skip and label `idle:needs-human` with the estimate as a comment. Otherwise proceed.
4. **Implement** — `implementer` agent works the ticket on a fresh isolated branch/worktree, fresh context each iteration (git + files are the memory), writing tests as it goes. Hard cap: `budget.max_iterations` per ticket.
5. **Guard** — run all guards in order; any failure → fail closed, park the work, move on (§6).
6. **Open PR** — generate a PR linking the issue, summarizing the change and which gates passed.
7. **Review** — `reviewer` agent (separate role) evaluates the diff against the issue's acceptance criteria → `approve` or `request_changes` with specific comments.
8. **Decide** — all guards green **and** review = approve **and** (if `merge.require_human`) human approval → merge. Else → park PR, label `idle:needs-human`, leave a crisp summary.
9. **Log & loop** — append the run record to the cost log (§8), then go to step 1. Stop if `budget.global_cap` is hit.

## 6. Components

### `idle_loop.py` — orchestrator
Drives the loop above. Owns config loading, the GitHub client, iteration accounting, global budget enforcement, and structured logging. Keep it thin — delegate real work to agents and guards.

### `agents/implementer.py`
Reads the issue (spec + acceptance criteria) and the repo, plans briefly, implements on a branch, writes/updates tests. Must operate within the scope allowlist (§6 guards). Fresh context per iteration.

### `agents/reviewer.py`
A distinct prompt/role. Input: the diff + the ticket's acceptance criteria. Output: structured verdict `{decision: approve|request_changes, comments: [...]}`. It checks *whether the change actually satisfies the ticket*, not just whether the code looks plausible. The reviewer must NOT be the same context/instance as the implementer.

### `guards/` (all fail-closed)
- **`scope.py`** — enforce max diff size and file count; enforce a path allowlist; hard-deny edits to secrets, CI config, infra/IaC, and DB migrations unless the ticket carries an explicit `idle:allow-sensitive` label.
- **`tests.py`** — run the test suite; require green. No tests present for new behavior → fail.
- **`security.py`** — scan the diff for leaked secrets/keys and obvious injection patterns (defensive only).
- **`budget.py`** — enforce per-ticket `max_iterations`, per-ticket dollar/token cap, and a no-progress detector (same error signature or empty diff `N` times in a row → bail).
- **`estimate.py`** — the pre-loop cost gate (§7).

## 7. Signature feature — `guards/estimate.py` (pre-loop cost estimation)

> **Provenance:** the pre-loop, per-ticket cost-estimation gate is this project's original contribution. Keep the attribution in the README.

**Purpose:** price a ticket *before* spending tokens, so tickets can be triaged by ROI.

**v1 — transparent heuristic.** Score each ticket on cheap, explainable proxies:
- number of acceptance criteria,
- estimated files/modules touched (keyword/path match of the ticket text against the repo tree),
- requires-new-tests flag,
- ambiguity penalty (vague spec → more iterations → higher cost),
- similarity to past tickets (reuse their measured cost if close).

Combine into `estimated_iterations`, then `estimated_cost = estimated_iterations × measured_avg_cost_per_iteration(this_repo, model)`.

**Output:** `{estimated_cost, estimated_iterations, confidence}` rendered honestly as a band, e.g. `~$30 ± $20`. Never present false precision.

**v2 — learned (stretch).** Every real run logs its pre-estimate features alongside actual cost/iterations (§8). Once enough rows exist, fit a simple regression on those features and swap it in behind the same interface. The estimator improves the more the loop runs.

**Gate behavior:** compare `estimated_cost` to `triage.auto_threshold`. Above → don't run; comment the estimate on the issue and label `idle:needs-human`. Below → proceed.

## 8. Cost log — `cost_log.jsonl`

One JSON object per processed ticket:

```
{ "ticket_id", "title", "features": { "n_criteria", "est_files", "needs_tests", "ambiguity_score" },
  "estimated_cost", "estimated_iterations", "actual_cost", "actual_iterations",
  "outcome": "merged|parked|skipped|failed", "timestamp" }
```

This file is both the audit trail and the training set for the v2 estimator.

## 9. Config — `idle.config.yaml`

```yaml
repo: "owner/name"
model: "claude-..."           # the coding agent model
labels:
  ready: "idle:ready"
  needs_human: "idle:needs-human"
  allow_sensitive: "idle:allow-sensitive"
triage:
  auto_threshold_usd: 50      # above this, park for human
budget:
  max_iterations: 20          # per ticket
  per_ticket_cap_usd: 40
  global_cap_usd: 200         # stop the whole loop here
guards:
  max_diff_lines: 400
  max_files: 15
  path_allowlist: ["src/**", "tests/**"]
  path_denylist: ["**/secrets/**", ".github/**", "infra/**", "**/migrations/**"]
merge:
  require_human: true         # default ON for protected branches
  target_branch: "main"
```

## 10. Repo structure

```
idle-loop/
  README.md                      # pitch, diagram, quickstart, "shipped by idle-loop" badge, estimator attribution
  idle.config.yaml
  idle_loop.py
  agents/   implementer.py  reviewer.py
  guards/   scope.py  tests.py  security.py  budget.py  estimate.py
  .github/workflows/idle-loop.yml   # scheduled / manual dispatch (agentic CI/CD)
  examples/                      # a tiny target repo + seeded idle:ready issues for an end-to-end demo
  cost_log.jsonl
```

## 11. Tech stack

- Python 3.11+.
- GitHub access via REST (issues, PRs, labels, merges).
- Anthropic API / Claude Code for the agents.
- `pytest` for the suite; `ruff`/`mypy` for lint/typecheck gates.
- No heavyweight framework — keep dependencies minimal.

## 12. Build order (milestones — build incrementally, each must pass its acceptance criteria before the next)

- **M0 — Scaffold.** Repo structure, config loader, GitHub client, logging.
  - *Done when:* `python idle_loop.py --dry-run` loads config and lists `idle:ready` issues without acting.
- **M1 — Happy path (no guards).** Discover → implement → open PR.
  - *Done when:* given one labeled issue, it opens a PR with the implementation on a branch.
- **M2 — Guards.** scope, tests, security, budget — all fail-closed.
  - *Done when:* a deliberately out-of-scope or test-failing change is blocked and the PR is parked with a reason.
- **M3 — Reviewer + merge gate.** Separate review agent; merge only on approve + green guards (+ human if required); park otherwise.
  - *Done when:* a passing ticket merges; a ticket failing acceptance criteria is parked with reviewer comments.
- **M4 — Estimator + triage gate.** Heuristic v1 prices every ticket; over-threshold tickets are parked pre-run.
  - *Done when:* each ticket prints `~$X ± $Y` before running, and expensive tickets are skipped with the estimate commented on the issue.
- **M5 — Cost logging + learned estimate (stretch).** Log features vs. actuals; fit v2 regression behind the same interface.
  - *Done when:* `cost_log.jsonl` accumulates rows and v2 can be toggled on via config.
- **M6 — Dogfood.** Convert `idle-loop`'s own roadmap into `idle:ready` issues and run the loop on itself.
  - *Done when:* at least one merged PR is authored by the loop, stamped "shipped by idle-loop."

## 13. Definition of done (v1)

Given a target repo with several `idle:ready` issues, running the loop: prints an honest cost estimate per ticket; processes affordable tickets; opens PRs; merges those passing all guards + review (+ human approval if configured); parks the rest with clear reasons; never exceeds the global budget cap; and writes a complete `cost_log.jsonl`.

## 14. Guardrails for the building agent (meta)

- Work **one milestone at a time**; do not start the next until the current milestone's acceptance criteria pass.
- Write tests for each component as you build it.
- Never edit secrets, CI, or infra unless the spec explicitly says to.
- Keep commits small — one logical change each.
- If the spec is ambiguous, leave `TODO(spec)` and a question; do not invent behavior.
