"""Tests for naming.py — branch / worktree naming and its inverse parsing."""

from __future__ import annotations

import naming
from models import Ticket


def _ticket(number=1, title="Add a thing", labels=()):
    return Ticket(number=number, title=title, body="", labels=list(labels))


# --------------------------------------------------------------------------- #
# prefix_for_ticket
# --------------------------------------------------------------------------- #
def test_prefix_defaults_to_feature():
    assert naming.prefix_for_ticket(_ticket(labels=["idle:ready"])) == "feature"


def test_prefix_from_labels():
    assert naming.prefix_for_ticket(_ticket(labels=["bug"])) == "bugfix"
    assert naming.prefix_for_ticket(_ticket(labels=["documentation"])) == "docs"
    assert naming.prefix_for_ticket(_ticket(labels=["chore"])) == "chore"
    assert naming.prefix_for_ticket(_ticket(labels=["enhancement"])) == "feature"


def test_prefix_from_title_conventional_type():
    assert naming.prefix_for_ticket(_ticket(title="fix: null deref")) == "bugfix"
    assert naming.prefix_for_ticket(_ticket(title="docs: update readme")) == "docs"
    assert naming.prefix_for_ticket(_ticket(title="feat(api): add route")) == "feature"
    assert naming.prefix_for_ticket(_ticket(title="chore!: bump deps")) == "chore"


def test_label_beats_title():
    # A "bug" label wins even when the title reads like a feature.
    t = _ticket(title="feat: shiny", labels=["bug"])
    assert naming.prefix_for_ticket(t) == "bugfix"


def test_plain_title_no_type_is_feature():
    assert naming.prefix_for_ticket(_ticket(title="Add a thing")) == "feature"


# --------------------------------------------------------------------------- #
# canonical_branch
# --------------------------------------------------------------------------- #
def test_canonical_branch_pattern():
    assert naming.canonical_branch(_ticket(number=7)) == "feature/issue-7"
    assert naming.canonical_branch(_ticket(number=9, labels=["bug"])) == "bugfix/issue-9"


# --------------------------------------------------------------------------- #
# dedupe_branch
# --------------------------------------------------------------------------- #
def test_dedupe_returns_base_when_free():
    assert naming.dedupe_branch("feature/issue-1", lambda _n: False) == "feature/issue-1"


def test_dedupe_appends_b_then_c():
    taken = {"feature/issue-1"}
    assert naming.dedupe_branch("feature/issue-1", lambda n: n in taken) == "feature/issue-1-b"
    taken.add("feature/issue-1-b")
    assert naming.dedupe_branch("feature/issue-1", lambda n: n in taken) == "feature/issue-1-c"


# --------------------------------------------------------------------------- #
# worktree naming + inverse
# --------------------------------------------------------------------------- #
def test_worktree_dir_name_flattens_one_slash():
    assert naming.worktree_dir_name("feature/issue-7") == "feature-issue-7"
    assert naming.worktree_dir_name("feature/issue-7-b") == "feature-issue-7-b"


def test_branch_from_worktree_dir_roundtrips():
    for branch in ("feature/issue-7", "bugfix/issue-3-b", "docs/issue-100", "chore/issue-2"):
        assert naming.branch_from_worktree_dir(naming.worktree_dir_name(branch)) == branch


def test_branch_from_worktree_dir_legacy_and_unknown():
    # Legacy "idle/" scheme still reaps.
    assert naming.branch_from_worktree_dir("idle-issue-9") == "idle/issue-9"
    assert naming.branch_from_worktree_dir("issue-5") == "idle/issue-5"
    # A directory we don't own.
    assert naming.branch_from_worktree_dir("some-random-dir") is None


# --------------------------------------------------------------------------- #
# issue_number_from_branch
# --------------------------------------------------------------------------- #
def test_issue_number_from_branch():
    assert naming.issue_number_from_branch("feature/issue-7") == 7
    assert naming.issue_number_from_branch("idle/issue-12-b") == 12
    assert naming.issue_number_from_branch("bugfix/issue-3-c") == 3
    assert naming.issue_number_from_branch("feature/not-ours") is None
    assert naming.issue_number_from_branch("") is None
