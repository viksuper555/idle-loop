# idle-loop — end-to-end demo

A tiny target repo plus seed issues so you can watch the whole loop run.

## What's here

- **`target_repo/`** — a minimal, pytest-green Python project (`src/calc.py` + `tests/`)
  that an `idle:ready` ticket can extend. It uses a `src/**` + `tests/**` layout that matches
  the default scope allowlist.
- **`seed_issues.md`** — three well-formed tickets (with acceptance criteria): two affordable,
  one deliberately vague-and-large so you can see the cost estimator park it for a human.
- **`seed_issues.sh`** — creates those issues (and the `idle:ready` label) on a repo via `gh`.

## Run it

```bash
# 0. Push examples/target_repo to its own GitHub repo, e.g. you/idle-demo.

# 1. Seed the demo issues onto it
REPO=you/idle-demo examples/seed_issues.sh

# 2. Point idle-loop at it
#    in idle.config.yaml:  repo: "you/idle-demo"

# 3. See what it would do — and what each ticket costs — without acting
python idle_loop.py --dry-run --repo-dir /path/to/idle-demo-clone

# Expected: the two affordable tickets print a band like "~$12 ± $6";
# the big vague one prints a higher band and, on a real run, is parked
# with the estimate commented on the issue.

# 4. Let it work the affordable ones (it parks the rest)
python idle_loop.py --repo-dir /path/to/idle-demo-clone --max-tickets 2
```

`--repo-dir` is the local working clone the implementer edits on a branch; `repo` in the
config is the `owner/name` the GitHub client talks to.

> These files describe/seed a **separate** demo repo. Do not add `idle:ready` issues to the
> `idle-loop` repo itself unless you intend to dogfood the loop on its own roadmap (milestone M6).
