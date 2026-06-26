# Seed issues for the demo

Three `idle:ready` tickets for the `target_repo`. Two are cheap and well-scoped; the third is
intentionally vague and large so the cost estimator parks it for a human.

`seed_issues.sh` creates exactly these via `gh`.

---

## #1 — Add a `multiply` function (cheap, clear)

Add a `multiply(a, b)` function to `src/calc.py`.

### Acceptance Criteria
- [ ] `multiply(a, b)` returns the product of two numbers.
- [ ] Handles negative numbers and zero.
- [ ] A unit test in `tests/test_calc.py` covers positive, negative, and zero cases.

---

## #2 — Add a `mean` helper with validation (cheap, clear)

Add a `mean(values)` function to `src/calc.py` that averages a list of numbers.

### Acceptance Criteria
- [ ] `mean(values)` returns the arithmetic mean of a non-empty list.
- [ ] Raises `ValueError` on an empty list.
- [ ] Tests cover a normal list and the empty-list error.

---

## #3 — Add a full statistics package + CLI (large → should be parked)

Add a statistics module with several functions and a command-line interface.

### Acceptance Criteria
- [ ] New `src/stats.py` module with `mean`, `median`, `mode`, `variance`, and `stdev` functions.
- [ ] Each function validates input and raises `ValueError` on an empty sequence.
- [ ] `mode` returns all modes when the data is multimodal.
- [ ] A new `src/cli.py` module exposes these functions via a command-line interface.
- [ ] The CLI prints results as formatted text.
- [ ] Unit tests for every function in `tests/test_stats.py`.
- [ ] Tests for the CLI in `tests/test_cli.py`.
- [ ] Edge cases (single element, negatives, floats) are covered.
- [ ] The README documents usage with examples.
- [ ] All new code has type hints and docstrings.

_(Ten acceptance criteria across multiple new files → a high estimated cost. With a demo
`triage.auto_threshold_usd` of ~25, idle-loop ships the two small tickets and **parks this one
pre-run**, commenting the estimate on the issue rather than burning budget on it.)_
