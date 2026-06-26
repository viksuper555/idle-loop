"""CI workflow trigger config — locks ticket #17 acceptance criteria."""

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "idle-loop.yml"


def _triggers() -> dict:
    doc = yaml.safe_load(WORKFLOW.read_text())
    # PyYAML parses the bare key `on:` as the boolean True (YAML 1.1).
    return doc[True]


def test_push_restricted_to_main() -> None:
    triggers = _triggers()
    assert triggers["push"] == {"branches": ["main"]}


def test_pull_request_still_runs() -> None:
    assert "pull_request" in _triggers()
