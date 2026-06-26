#!/usr/bin/env bash
# Seed the idle-loop demo issues onto a target repo.
#
#   REPO=owner/name examples/seed_issues.sh
#
# Creates the idle:ready label and three issues (two affordable, one that the
# cost estimator should park). Requires an authenticated `gh` CLI.
set -euo pipefail

REPO="${REPO:?set REPO=owner/name (the target repo to seed)}"

echo "Seeding $REPO ..."

# Labels (idempotent — ignore "already exists").
gh label create "idle:ready"          --repo "$REPO" --color 0e8a16 --description "Ready for idle-loop" 2>/dev/null || true
gh label create "idle:needs-human"    --repo "$REPO" --color b60205 --description "Parked by idle-loop" 2>/dev/null || true
gh label create "idle:allow-sensitive" --repo "$REPO" --color fbca04 --description "Permit sensitive-path edits" 2>/dev/null || true

gh issue create --repo "$REPO" --label "idle:ready" \
  --title "Add a multiply function to calc" \
  --body "Add a \`multiply(a, b)\` function to \`src/calc.py\`.

## Acceptance Criteria
- [ ] \`multiply(a, b)\` returns the product of two numbers.
- [ ] Handles negative numbers and zero.
- [ ] A unit test in \`tests/test_calc.py\` covers positive, negative, and zero cases."

gh issue create --repo "$REPO" --label "idle:ready" \
  --title "Add a mean helper with input validation" \
  --body "Add a \`mean(values)\` function to \`src/calc.py\` that averages a list of numbers.

## Acceptance Criteria
- [ ] \`mean(values)\` returns the arithmetic mean of a non-empty list.
- [ ] Raises \`ValueError\` on an empty list.
- [ ] Tests cover a normal list and the empty-list error."

gh issue create --repo "$REPO" --label "idle:ready" \
  --title "Add a full statistics package + CLI" \
  --body "Add a statistics module with several functions and a command-line interface.

## Acceptance Criteria
- [ ] New \`src/stats.py\` module with \`mean\`, \`median\`, \`mode\`, \`variance\`, and \`stdev\` functions.
- [ ] Each function validates input and raises \`ValueError\` on an empty sequence.
- [ ] \`mode\` returns all modes when the data is multimodal.
- [ ] A new \`src/cli.py\` module exposes these functions via a command-line interface.
- [ ] The CLI prints results as formatted text.
- [ ] Unit tests for every function in \`tests/test_stats.py\`.
- [ ] Tests for the CLI in \`tests/test_cli.py\`.
- [ ] Edge cases (single element, negatives, floats) are covered.
- [ ] The README documents usage with examples.
- [ ] All new code has type hints and docstrings."

echo "Done. For a clean triage demo, set triage.auto_threshold_usd: 25 in idle.config.yaml,"
echo "point repo at $REPO, then run:  python idle_loop.py --dry-run"
