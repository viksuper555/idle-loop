"""Guard the idle-loop CI triggers (ticket #17).

CI must run on push ONLY to main, while still running on PRs.
"""

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "idle-loop.yml"


def _load():
    return yaml.safe_load(WORKFLOW.read_text())


def test_workflow_exists():
    assert WORKFLOW.is_file(), f"missing workflow file: {WORKFLOW}"


def test_triggers_block_present():
    # PyYAML parses the bare `on:` key as the boolean True.
    wf = _load()
    triggers = wf.get("on", wf.get(True))
    assert triggers is not None, "no `on:` trigger block"


def test_push_restricted_to_main():
    wf = _load()
    triggers = wf.get("on", wf.get(True))
    push = triggers.get("push")
    assert push is not None, "push trigger removed — CI would not run on push to main"
    assert push.get("branches") == ["main"], (
        f"push must be limited to main, got {push.get('branches')!r}"
    )


def test_pull_request_still_runs():
    wf = _load()
    triggers = wf.get("on", wf.get(True))
    assert "pull_request" in triggers, "CI must still run on PRs"
