"""GitHub REST API client for idle-loop.

A thin, fail-loud wrapper over ``api.github.com`` built on a single
:class:`requests.Session`. The orchestrator uses it to discover ready issues,
comment, manage labels, open PRs, inspect their checks, and merge.

stdlib + ``requests`` only. Token resolution order:

1. the ``token`` argument,
2. ``GITHUB_TOKEN`` / ``GH_TOKEN`` environment variables,
3. a best-effort ``gh auth token`` shell-out (failure is swallowed -> ``None``).

Every request goes through :meth:`GitHubClient._request`, which raises
:class:`GitHubError` on any non-2xx status (including the response body for
debuggability).
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

import requests

from models import Ticket

API_BASE = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "idle-loop"


class GitHubError(RuntimeError):
    """Raised when the GitHub API returns a non-2xx response."""


def _resolve_token(token: str | None) -> str | None:
    """Resolve an API token from arg, env, then ``gh auth token``."""
    if token:
        return token
    env = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if env:
        return env
    try:
        out = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        resolved = out.stdout.strip()
        return resolved or None
    except Exception:  # noqa: BLE001 - best-effort; any failure means no token
        return None


class GitHubClient:
    """Talks to the GitHub REST API for one target repository."""

    def __init__(self, repo: str, token: str | None = None) -> None:
        """Create a client for ``repo`` ("owner/name").

        The token is resolved eagerly (see module docstring) and, when present,
        attached as a Bearer credential on the underlying session.
        """
        self.repo = repo
        self.token = _resolve_token(token)
        self.session = requests.Session()
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self.session.headers.update(headers)

    # ------------------------------------------------------------------ #
    # Low-level
    # ------------------------------------------------------------------ #
    def _request(self, method: str, path: str, **kw: Any) -> requests.Response:
        """Issue a request, raising :class:`GitHubError` on any non-2xx status.

        ``path`` may be an absolute URL or a path relative to the API base.
        """
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        resp = self.session.request(method, url, **kw)
        if not 200 <= resp.status_code < 300:
            raise GitHubError(
                f"{method} {url} -> {resp.status_code}: {resp.text}"
            )
        return resp

    # ------------------------------------------------------------------ #
    # Issues
    # ------------------------------------------------------------------ #
    def list_ready_issues(self, label: str) -> list[Ticket]:
        """List open issues carrying ``label``, oldest first.

        The issues endpoint also returns pull requests; entries with a
        ``pull_request`` key are skipped so only true issues become tickets.
        """
        resp = self._request(
            "GET",
            f"/repos/{self.repo}/issues",
            params={
                "state": "open",
                "labels": label,
                "sort": "created",
                "direction": "asc",
            },
        )
        issues = resp.json()
        return [
            Ticket.from_issue(issue)
            for issue in issues
            if "pull_request" not in issue
        ]

    def get_issue(self, number: int) -> Ticket:
        """Fetch a single issue as a :class:`Ticket`."""
        resp = self._request("GET", f"/repos/{self.repo}/issues/{number}")
        return Ticket.from_issue(resp.json())

    def comment(self, number: int, body: str) -> None:
        """Post a comment on issue ``number``."""
        self._request(
            "POST",
            f"/repos/{self.repo}/issues/{number}/comments",
            json={"body": body},
        )

    def list_comments(self, number: int) -> list[dict]:
        """List the comments on issue ``number`` (each ``{"id", "body", ...}``)."""
        resp = self._request(
            "GET",
            f"/repos/{self.repo}/issues/{number}/comments",
            params={"per_page": 100},
        )
        return resp.json()

    def update_comment(self, comment_id: int, body: str) -> None:
        """Edit the body of an existing issue comment in place."""
        self._request(
            "PATCH",
            f"/repos/{self.repo}/issues/comments/{comment_id}",
            json={"body": body},
        )

    def upsert_comment(self, number: int, marker: str, body: str) -> None:
        """Create or update a single sticky comment identified by ``marker``.

        Finds the first existing comment whose body contains ``marker`` and edits
        it in place; otherwise posts ``body`` as a new comment. ``body`` is
        expected to already carry ``marker`` so future calls can find it.
        """
        for comment in self.list_comments(number):
            if marker in (comment.get("body") or ""):
                self.update_comment(comment["id"], body)
                return
        self.comment(number, body)

    def add_label(self, number: int, label: str) -> None:
        """Add ``label`` to issue ``number``."""
        self._request(
            "POST",
            f"/repos/{self.repo}/issues/{number}/labels",
            json={"labels": [label]},
        )

    def remove_label(self, number: int, label: str) -> None:
        """Remove ``label`` from issue ``number``; a missing label is fine."""
        url = f"{API_BASE}/repos/{self.repo}/issues/{number}/labels/{label}"
        resp = self.session.request("DELETE", url)
        if resp.status_code == 404:
            return
        if not 200 <= resp.status_code < 300:
            raise GitHubError(
                f"DELETE {url} -> {resp.status_code}: {resp.text}"
            )

    # ------------------------------------------------------------------ #
    # Labels
    # ------------------------------------------------------------------ #
    def ensure_labels(self, names: list[str]) -> None:
        """Create any of ``names`` not already defined on the repo.

        Best-effort: an "already exists" 422 from a concurrent create is
        ignored rather than raised.
        """
        resp = self._request("GET", f"/repos/{self.repo}/labels")
        existing = {lbl.get("name") for lbl in resp.json()}
        for name in names:
            if name in existing:
                continue
            create = self.session.request(
                "POST",
                f"{API_BASE}/repos/{self.repo}/labels",
                json={"name": name},
            )
            if create.status_code == 422:
                continue
            if not 200 <= create.status_code < 300:
                raise GitHubError(
                    f"POST /repos/{self.repo}/labels -> "
                    f"{create.status_code}: {create.text}"
                )

    # ------------------------------------------------------------------ #
    # Pull requests
    # ------------------------------------------------------------------ #
    def create_pull_request(
        self, title: str, head: str, base: str, body: str
    ) -> dict:
        """Open a PR and return ``{"number": int, "html_url": str}``."""
        resp = self._request(
            "POST",
            f"/repos/{self.repo}/pulls",
            json={"title": title, "head": head, "base": base, "body": body},
        )
        data = resp.json()
        return {"number": data["number"], "html_url": data["html_url"]}

    def pr_status_for_branch(self, branch: str) -> str:
        """Classify the PR lifecycle for head ``branch``.

        Returns ``"open"`` if an open PR exists, ``"done"`` if one or more PRs
        exist but none are open (merged or closed), or ``"none"`` if the branch
        never had a PR. Used to decide a ticket worktree's fate: keep it while
        ``"open"`` (work may resume), reclaim it once ``"done"``, and leave it
        alone while ``"none"`` (implementation may be mid-flight, pre-PR).
        """
        owner = self.repo.split("/", 1)[0]
        resp = self._request(
            "GET",
            f"/repos/{self.repo}/pulls",
            params={"head": f"{owner}:{branch}", "state": "all"},
        )
        prs = resp.json()
        if not prs:
            return "none"
        if any(pr.get("state") == "open" for pr in prs):
            return "open"
        return "done"

    def pull_request_checks_passing(self, number: int) -> bool:
        """Return whether all check-runs on the PR's head commit pass.

        Passing means: zero checks, or every check's conclusion is one of
        ``success``, ``neutral``, ``skipped``. Any check with a missing or
        unlisted conclusion (e.g. still ``in_progress``, or ``failure``) makes
        the PR not-passing.
        """
        pr = self._request("GET", f"/repos/{self.repo}/pulls/{number}").json()
        sha = pr.get("head", {}).get("sha")
        if not sha:
            return False
        runs = self._request(
            "GET", f"/repos/{self.repo}/commits/{sha}/check-runs"
        ).json()
        checks = runs.get("check_runs", [])
        if not checks:
            return True
        allowed = {"success", "neutral", "skipped"}
        return all(check.get("conclusion") in allowed for check in checks)

    def merge_pull_request(self, number: int, method: str = "squash") -> bool:
        """Merge PR ``number`` via ``method``; return GitHub's ``merged`` flag."""
        resp = self._request(
            "PUT",
            f"/repos/{self.repo}/pulls/{number}/merge",
            json={"merge_method": method},
        )
        return bool(resp.json().get("merged", False))

    # ------------------------------------------------------------------ #
    # Repo
    # ------------------------------------------------------------------ #
    def default_branch(self) -> str:
        """Return the repository's default branch name."""
        resp = self._request("GET", f"/repos/{self.repo}")
        return resp.json()["default_branch"]
