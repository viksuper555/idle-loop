# Progress — #19: committed branch is portable memory (PROGRESS.md); demote --resume

## Done
- `agents/implementer.py`: added `PROGRESS.md` helpers (`progress_path`, `load_progress`, `write_progress`); the prompt now carries the current PROGRESS.md on every invocation; a `_PROGRESS_RULE` (prose-only, no secrets, resume-not-required) is appended to the system prompt; after a successful turn the implementer authors/refreshes PROGRESS.md and commits it on the branch.
- `agents/planner.py`: seeds the first PROGRESS.md entry from the plan text (prose only).
- Tests added across `tests/test_implementer.py` and `tests/test_planner.py`: cold start with `resume=None`, PROGRESS.md written+committed after a turn, prompt includes committed contents, reviewer asks recorded on revision, prose-only rule stated, planner seeding, helper roundtrip.

## Remaining
- Nothing outstanding; acceptance criteria met. A later ticket strips PROGRESS.md before merge.

## Current approach
The implementer code itself guarantees a committed, prose-only PROGRESS.md (synthesised from structured data, never raw output) so continuity travels in git; `--resume` is kept only as a same-machine cache via `resume_session_id`. The harness agent is also instructed to maintain PROGRESS.md.

## Files touched
- agents/implementer.py
- agents/planner.py
- tests/test_implementer.py
- tests/test_planner.py

## Last reviewer asks
(none — no reviewer feedback yet)
