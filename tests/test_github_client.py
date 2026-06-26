"""Tests for github_client.GitHubClient.

Every external boundary is mocked: the ``requests.Session.request`` method is
monkeypatched to a recorder returning fake responses, and the ``gh auth token``
subprocess is patched. No network, no real subprocess.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import github_client
from github_client import GitHubClient, GitHubError
from models import Ticket


class FakeResponse:
    """Stand-in for ``requests.Response`` exposing the bits we use."""

    def __init__(self, status_code: int, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self) -> Any:
        return self._payload


class RecordingSession:
    """Records each ``request(...)`` call and replays queued responses."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.headers: dict[str, str] = {}
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    def update(self, headers: dict[str, str]) -> None:  # session.headers.update
        self.headers.update(headers)

    def request(self, method: str, url: str, **kw: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kw})
        return self._responses.pop(0)


@pytest.fixture(autouse=True)
def _no_token_subprocess(monkeypatch):
    """Prevent any real ``gh auth token`` shell-out during construction."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    def _boom(*_a, **_k):  # pragma: no cover - should be short-circuited by token arg
        raise AssertionError("subprocess should not run when a token is provided")

    monkeypatch.setattr(github_client.subprocess, "run", _boom)


def make_client(monkeypatch, responses: list[FakeResponse]) -> tuple[GitHubClient, RecordingSession]:
    """Build a client whose session is a RecordingSession with queued responses."""
    session = RecordingSession(responses)
    # session.headers is a plain object exposing .update — RecordingSession itself.
    monkeypatch.setattr(github_client.requests, "Session", lambda: _SessionShim(session))
    client = GitHubClient("owner/name", token="t0ken")
    return client, session


class _SessionShim:
    """Wires RecordingSession so ``.headers.update`` and ``.request`` both work."""

    def __init__(self, rec: RecordingSession) -> None:
        self._rec = rec
        self.headers = rec  # .update() lives on RecordingSession

    def request(self, method: str, url: str, **kw: Any) -> FakeResponse:
        return self._rec.request(method, url, **kw)


# --------------------------------------------------------------------------- #
# Token resolution + headers
# --------------------------------------------------------------------------- #
def test_token_from_arg_sets_bearer_header(monkeypatch):
    client, session = make_client(monkeypatch, [])
    assert client.token == "t0ken"
    assert session.headers["Authorization"] == "Bearer t0ken"
    assert session.headers["Accept"] == "application/vnd.github+json"
    assert session.headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert session.headers["User-Agent"] == "idle-loop"


def test_token_from_env(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "envtok")
    session = RecordingSession([])
    monkeypatch.setattr(github_client.requests, "Session", lambda: _SessionShim(session))
    client = GitHubClient("owner/name")
    assert client.token == "envtok"
    assert session.headers["Authorization"] == "Bearer envtok"


def test_token_from_gh_cli(monkeypatch):
    class _Out:
        stdout = "  clitok\n"

    monkeypatch.setattr(github_client.subprocess, "run", lambda *a, **k: _Out())
    session = RecordingSession([])
    monkeypatch.setattr(github_client.requests, "Session", lambda: _SessionShim(session))
    client = GitHubClient("owner/name")
    assert client.token == "clitok"


def test_token_missing_when_gh_fails(monkeypatch):
    def _boom(*_a, **_k):
        raise FileNotFoundError("no gh")

    monkeypatch.setattr(github_client.subprocess, "run", _boom)
    session = RecordingSession([])
    monkeypatch.setattr(github_client.requests, "Session", lambda: _SessionShim(session))
    client = GitHubClient("owner/name")
    assert client.token is None
    assert "Authorization" not in session.headers


# --------------------------------------------------------------------------- #
# Issues
# --------------------------------------------------------------------------- #
def test_list_ready_issues_builds_request_and_filters_prs(monkeypatch):
    payload = [
        {"number": 1, "title": "real issue", "body": "do it", "labels": [{"name": "idle:ready"}]},
        {"number": 2, "title": "a PR", "body": "", "pull_request": {"url": "x"}},
        {"number": 3, "title": "another issue", "body": "", "labels": []},
    ]
    client, session = make_client(monkeypatch, [FakeResponse(200, payload)])

    tickets = client.list_ready_issues("idle:ready")

    assert [t.number for t in tickets] == [1, 3]
    assert all(isinstance(t, Ticket) for t in tickets)
    call = session.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "https://api.github.com/repos/owner/name/issues"
    assert call["params"] == {
        "state": "open",
        "labels": "idle:ready",
        "sort": "created",
        "direction": "asc",
    }


def test_get_issue_parses_ticket(monkeypatch):
    payload = {
        "number": 7,
        "title": "T",
        "body": "## Acceptance Criteria\n- [ ] one\n- [ ] two\n",
        "labels": [{"name": "bug"}],
        "html_url": "https://github.com/owner/name/issues/7",
    }
    client, session = make_client(monkeypatch, [FakeResponse(200, payload)])

    ticket = client.get_issue(7)

    assert ticket.number == 7
    assert ticket.labels == ["bug"]
    assert ticket.acceptance_criteria == ["one", "two"]
    assert session.calls[0]["url"] == "https://api.github.com/repos/owner/name/issues/7"


def test_comment_posts_body(monkeypatch):
    client, session = make_client(monkeypatch, [FakeResponse(201, {"id": 1})])
    client.comment(7, "hello")
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api.github.com/repos/owner/name/issues/7/comments"
    assert call["json"] == {"body": "hello"}


def test_list_comments_paginates(monkeypatch):
    payload = [{"id": 1, "body": "a"}, {"id": 2, "body": "b"}]
    client, session = make_client(monkeypatch, [FakeResponse(200, payload)])
    assert client.list_comments(7) == payload
    call = session.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "https://api.github.com/repos/owner/name/issues/7/comments"
    assert call["params"] == {"per_page": 100}


def test_update_comment_patches_by_id(monkeypatch):
    client, session = make_client(monkeypatch, [FakeResponse(200, {"id": 5})])
    client.update_comment(5, "edited")
    call = session.calls[0]
    assert call["method"] == "PATCH"
    assert call["url"] == "https://api.github.com/repos/owner/name/issues/comments/5"
    assert call["json"] == {"body": "edited"}


def test_upsert_comment_updates_existing_marked_comment(monkeypatch):
    marker = "<!-- m -->"
    client, session = make_client(
        monkeypatch,
        [
            FakeResponse(200, [{"id": 9, "body": f"{marker}\nold"}]),  # list
            FakeResponse(200, {"id": 9}),  # patch
        ],
    )
    client.upsert_comment(7, marker, f"{marker}\nnew")
    assert session.calls[0]["method"] == "GET"
    assert session.calls[1]["method"] == "PATCH"
    assert session.calls[1]["url"] == "https://api.github.com/repos/owner/name/issues/comments/9"
    assert session.calls[1]["json"] == {"body": f"{marker}\nnew"}


def test_upsert_comment_creates_when_absent(monkeypatch):
    marker = "<!-- m -->"
    client, session = make_client(
        monkeypatch,
        [
            FakeResponse(200, [{"id": 9, "body": "unrelated"}]),  # list -> no marker
            FakeResponse(201, {"id": 10}),  # create
        ],
    )
    client.upsert_comment(7, marker, f"{marker}\nfresh")
    assert session.calls[0]["method"] == "GET"
    assert session.calls[1]["method"] == "POST"
    assert session.calls[1]["url"] == "https://api.github.com/repos/owner/name/issues/7/comments"
    assert session.calls[1]["json"] == {"body": f"{marker}\nfresh"}


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
def test_request_raises_on_500_with_body(monkeypatch):
    client, _ = make_client(monkeypatch, [FakeResponse(500, None, text="boom")])
    with pytest.raises(GitHubError) as exc:
        client.get_issue(1)
    assert "500" in str(exc.value)
    assert "boom" in str(exc.value)


# --------------------------------------------------------------------------- #
# Labels
# --------------------------------------------------------------------------- #
def test_add_label(monkeypatch):
    client, session = make_client(monkeypatch, [FakeResponse(200, [])])
    client.add_label(3, "idle:needs-human")
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api.github.com/repos/owner/name/issues/3/labels"
    assert call["json"] == {"labels": ["idle:needs-human"]}


def test_remove_label_tolerates_404(monkeypatch):
    client, session = make_client(monkeypatch, [FakeResponse(404, {"message": "nope"})])
    client.remove_label(3, "gone")  # must not raise
    call = session.calls[0]
    assert call["method"] == "DELETE"
    assert call["url"] == "https://api.github.com/repos/owner/name/issues/3/labels/gone"


def test_remove_label_raises_on_other_error(monkeypatch):
    client, _ = make_client(monkeypatch, [FakeResponse(500, None, text="oops")])
    with pytest.raises(GitHubError):
        client.remove_label(3, "x")


def test_remove_label_success(monkeypatch):
    client, _ = make_client(monkeypatch, [FakeResponse(200, [])])
    client.remove_label(3, "ok")  # no raise


def test_ensure_labels_creates_missing_and_ignores_existing(monkeypatch):
    existing = [{"name": "bug"}, {"name": "idle:ready"}]
    client, session = make_client(
        monkeypatch,
        [
            FakeResponse(200, existing),  # GET labels
            FakeResponse(201, {"name": "idle:needs-human"}),  # create missing
        ],
    )
    client.ensure_labels(["bug", "idle:needs-human"])
    # First call: GET labels; second: POST create for the missing one only.
    assert session.calls[0]["method"] == "GET"
    assert session.calls[1]["method"] == "POST"
    assert session.calls[1]["json"] == {"name": "idle:needs-human"}
    assert len(session.calls) == 2


def test_ensure_labels_ignores_422(monkeypatch):
    client, session = make_client(
        monkeypatch,
        [
            FakeResponse(200, []),  # GET labels -> none exist
            FakeResponse(422, {"message": "already_exists"}),  # create races
        ],
    )
    client.ensure_labels(["dup"])  # 422 swallowed, no raise
    assert len(session.calls) == 2


# --------------------------------------------------------------------------- #
# Pull requests
# --------------------------------------------------------------------------- #
def test_create_pull_request(monkeypatch):
    client, session = make_client(
        monkeypatch,
        [FakeResponse(201, {"number": 42, "html_url": "https://gh/pr/42", "extra": 1})],
    )
    out = client.create_pull_request("title", "feat", "main", "body")
    assert out == {"number": 42, "html_url": "https://gh/pr/42"}
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api.github.com/repos/owner/name/pulls"
    assert call["json"] == {"title": "title", "head": "feat", "base": "main", "body": "body"}


def test_checks_passing_no_checks_is_true(monkeypatch):
    client, _ = make_client(
        monkeypatch,
        [
            FakeResponse(200, {"head": {"sha": "abc"}}),
            FakeResponse(200, {"check_runs": []}),
        ],
    )
    assert client.pull_request_checks_passing(1) is True


def test_checks_passing_all_success(monkeypatch):
    client, _ = make_client(
        monkeypatch,
        [
            FakeResponse(200, {"head": {"sha": "abc"}}),
            FakeResponse(
                200,
                {"check_runs": [{"conclusion": "success"}, {"conclusion": "skipped"}]},
            ),
        ],
    )
    assert client.pull_request_checks_passing(1) is True


def test_checks_failing_on_bad_conclusion(monkeypatch):
    client, _ = make_client(
        monkeypatch,
        [
            FakeResponse(200, {"head": {"sha": "abc"}}),
            FakeResponse(
                200,
                {"check_runs": [{"conclusion": "success"}, {"conclusion": "failure"}]},
            ),
        ],
    )
    assert client.pull_request_checks_passing(1) is False


def test_checks_failing_on_in_progress(monkeypatch):
    client, _ = make_client(
        monkeypatch,
        [
            FakeResponse(200, {"head": {"sha": "abc"}}),
            FakeResponse(200, {"check_runs": [{"conclusion": None}]}),
        ],
    )
    assert client.pull_request_checks_passing(1) is False


def test_checks_failing_when_no_sha(monkeypatch):
    client, _ = make_client(monkeypatch, [FakeResponse(200, {"head": {}})])
    assert client.pull_request_checks_passing(1) is False


def test_merge_pull_request(monkeypatch):
    client, session = make_client(monkeypatch, [FakeResponse(200, {"merged": True})])
    assert client.merge_pull_request(5, method="rebase") is True
    call = session.calls[0]
    assert call["method"] == "PUT"
    assert call["url"] == "https://api.github.com/repos/owner/name/pulls/5/merge"
    assert call["json"] == {"merge_method": "rebase"}


def test_merge_pull_request_not_merged(monkeypatch):
    client, _ = make_client(monkeypatch, [FakeResponse(200, {"merged": False})])
    assert client.merge_pull_request(5) is False


def test_default_branch(monkeypatch):
    client, session = make_client(monkeypatch, [FakeResponse(200, {"default_branch": "trunk"})])
    assert client.default_branch() == "trunk"
    assert session.calls[0]["url"] == "https://api.github.com/repos/owner/name"


def test_pr_status_for_branch_open(monkeypatch):
    client, session = make_client(
        monkeypatch, [FakeResponse(200, [{"number": 9, "state": "open"}])]
    )
    assert client.pr_status_for_branch("idle/issue-3") == "open"
    call = session.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "https://api.github.com/repos/owner/name/pulls"
    assert call["params"] == {"head": "owner:idle/issue-3", "state": "all"}


def test_pr_status_for_branch_done(monkeypatch):
    client, _ = make_client(
        monkeypatch, [FakeResponse(200, [{"number": 9, "state": "closed"}])]
    )
    assert client.pr_status_for_branch("idle/issue-3") == "done"


def test_pr_status_for_branch_none(monkeypatch):
    client, _ = make_client(monkeypatch, [FakeResponse(200, [])])
    assert client.pr_status_for_branch("idle/issue-3") == "none"
