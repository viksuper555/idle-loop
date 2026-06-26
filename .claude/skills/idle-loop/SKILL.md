---
name: idle-loop
description: >-
  Operate the idle-loop autonomous coding loop from a Claude Code session —
  price/triage idle:ready tickets, sync the idle:* labels, run a bounded pass,
  or start the watch listener. Use when the user asks to run idle-loop, estimate
  a ticket's cost, sync idle labels, or work the idle board in this repo.
---

# Operating idle-loop

idle-loop watches a GitHub repo's `idle:ready` issues and runs each through:
estimate cost → triage → implement on a branch (via the Claude Code harness) →
fail-closed guards → open PR → separate reviewer → merge or park. This skill is
the **manual / interactive** front door; the unattended path is
`./idle-listener.sh` + cron (see the README).

## First: orient, don't assume

1. Read `idle.config.yaml` — note `repo:` (the **target** repo the loop works on)
   and `merge.require_human` (default `true` → it opens PRs and parks for human
   approval, never auto-merges).
2. Confirm prerequisites without leaking secrets:
   - `claude --version` (the harness; auth is the user's Claude login — **no API key**),
   - a GitHub token is available (`gh auth status`, or `GITHUB_TOKEN`/`GH_TOKEN` set),
   - the target repo has issues labeled `idle:ready`, each with an
     `## Acceptance Criteria` checklist (tickets without criteria are rejected).
3. Ask the user which they want if it isn't explicit: **estimate only**, **sync
   labels**, **work N tickets**, or **start watching**. Ask via the GitHub
   checkbox protocol below — not the in-terminal prompt.

## Asking the user — via GitHub, not the terminal

The user operates this loop through GitHub, not the Claude Code terminal. So
**every** decision you'd otherwise raise with `AskUserQuestion` is instead posted
as a GitHub issue comment with task-list checkboxes, and you read the answer back
from the ticked box.

1. Post a comment on the relevant `idle:ready` issue (the one in question; if the
   choice is board-wide, use the lowest-numbered ready issue). One option per
   `- [ ]` line, an instruction to tick exactly one, and any caveats below a `---`.
2. Poll the comment until a box flips to `- [x]`:
   `gh api repos/<repo>/issues/comments/<id> --jq .body`. Run it in the background
   so the session stays responsive, with a **backoff cadence**: every 30s for the
   first 10 min, then every 1 min until 30 min, then every 5 min thereafter (cap
   ~2 h).
3. Act on the ticked option. If the window times out with nothing ticked, say so
   and re-post or wait — don't assume a default.
4. Only fall back to an in-terminal prompt if GitHub is unreachable (`gh` errors).

## Commands (run from the repo root)

| Goal | Command |
|------|---------|
| Price every ready ticket, take **no** action | `python idle_loop.py --dry-run` |
| Create/sync the `idle:*` labels (token only) | `python idle_loop.py --ensure-labels` |
| Work at most N tickets, then stop | `python idle_loop.py --repo-dir <local clone of target> --max-tickets N` |
| Watch the board continuously | `./idle-listener.sh --watch --interval 900 -- --repo-dir <clone>` |

- `repo:` in the config is the `owner/name` for the GitHub API; `--repo-dir` is a
  **local clone** of that repo where the implementer commits on branch
  `idle/issue-N`. For dogfooding this repo, they can be the same checkout.
- **Default to `--dry-run` first.** Show the user the cost band per ticket
  (`~$X ± $Y`) and which tickets would be parked over `triage.auto_threshold_usd`
  before spending anything.
- A real pass spends real usage: the implementer is `claude -p`, which draws on
  the **same usage window** as this session. Bound it with `--max-tickets` and
  confirm with the user before a non-dry run.

## Reading the result

- Per ticket the loop logs the estimate, then `merged` / `parked` / `skipped` /
  `failed`, and appends a row to `cost_log.jsonl` (features + estimated vs actual
  cost — the estimator's training set).
- Parked tickets get the `idle:needs-human` label and a comment with the reason
  (failing guard, reviewer's requested changes, or "human approval required").
- On a rate limit the loop exits 42 and writes `.idle-loop/rate_limit.json`; that
  is the listener's cue to reschedule — don't treat exit 42 as a crash.

## Don't

- Don't bypass the guards or flip `merge.require_human` to `false` without the
  user explicitly asking — the human-approval gate is the safety default.
- Don't run an unbounded real pass; always pass `--max-tickets` for a manual run.
- Don't hand-edit `cost_log.jsonl`.
