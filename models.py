"""Shared data models for idle-loop.

These dataclasses are the contracts every component conforms to. Keep them
dependency-free (stdlib only) so guards, agents, the GitHub client, and the
orchestrator can all import them without cycles.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class Outcome(StrEnum):
    """Terminal outcome for a processed ticket (written to the cost log)."""

    MERGED = "merged"
    PARKED = "parked"
    SKIPPED = "skipped"
    FAILED = "failed"


class Decision(StrEnum):
    """Reviewer verdict."""

    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"


# --------------------------------------------------------------------------- #
# Tickets
# --------------------------------------------------------------------------- #
_AC_HEADER = re.compile(
    r"^\s*(?:#{1,6}\s*)?(acceptance criteria|acceptance|ac|done when|definition of done)\s*:?\s*$",
    re.IGNORECASE,
)
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")
_CHECKBOX = re.compile(r"^\s*(?:[-*+]\s*)?\[[ xX]\]\s+(.*\S)\s*$")


def parse_acceptance_criteria(body: str) -> list[str]:
    """Extract acceptance-criteria bullet lines from an issue body.

    Looks for a heading like ``## Acceptance Criteria`` (or "Done when",
    "Definition of Done") and collects the bullet/checkbox list that follows,
    stopping at the next heading or a blank-line-separated non-list block.

    Falls back to collecting every checkbox line anywhere in the body if no
    explicit header is present.
    """
    lines = body.splitlines()
    criteria: list[str] = []
    in_section = False
    for line in lines:
        if _AC_HEADER.match(line):
            in_section = True
            continue
        if in_section:
            if re.match(r"^\s*#{1,6}\s+\S", line):  # next heading ends the section
                break
            m = _CHECKBOX.match(line) or _BULLET.match(line)
            if m:
                criteria.append(m.group(1).strip())
            elif line.strip() == "":
                continue
            else:
                # A non-list, non-blank line ends a loosely-formatted section.
                break
    if not criteria:
        # Fallback: any checkbox anywhere counts as a criterion.
        for line in lines:
            m = _CHECKBOX.match(line)
            if m:
                criteria.append(m.group(1).strip())
    return criteria


@dataclass
class Ticket:
    """A GitHub issue selected for processing."""

    number: int
    title: str
    body: str
    labels: list[str] = field(default_factory=list)
    url: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)

    @classmethod
    def from_issue(cls, issue: dict) -> Ticket:
        """Build a Ticket from a GitHub REST issue object."""
        labels = [
            lbl["name"] if isinstance(lbl, dict) else str(lbl)
            for lbl in issue.get("labels", [])
        ]
        body = issue.get("body") or ""
        return cls(
            number=int(issue["number"]),
            title=issue.get("title", ""),
            body=body,
            labels=labels,
            url=issue.get("html_url", issue.get("url", "")),
            acceptance_criteria=parse_acceptance_criteria(body),
        )

    def has_label(self, name: str) -> bool:
        return name in self.labels


# --------------------------------------------------------------------------- #
# Estimation
# --------------------------------------------------------------------------- #
@dataclass
class EstimateFeatures:
    """Cheap, explainable proxies scored before any tokens are spent."""

    n_criteria: int = 0
    est_files: int = 0
    needs_tests: bool = False
    ambiguity_score: float = 0.0  # 0 (clear) .. 1 (vague)
    similarity_cost: float | None = None  # measured cost of a near-identical past ticket

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EstimateResult:
    """Output of the pre-loop cost gate, rendered as an honest band."""

    estimated_cost: float
    estimated_iterations: float
    confidence: float  # 0..1
    features: EstimateFeatures
    margin: float = 0.0  # +/- dollar band
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    source: str = "heuristic"  # "planner" (deterministic) | "heuristic" (fallback)

    def band(self) -> str:
        """Render as ``~$30 ± $20`` — never false precision."""
        center = int(round(self.estimated_cost))
        margin = int(round(self.margin))
        if margin <= 0:
            return f"~${center}"
        return f"~${center} ± ${margin}"

    @property
    def estimated_tokens(self) -> int:
        """Total estimated token budget (0 when the estimate is heuristic)."""
        return self.estimated_input_tokens + self.estimated_output_tokens


def format_tokens(n: int) -> str:
    """Compact human token count: 950 -> '950', 46_000 -> '46k', 1_230_000 -> '1.23M'."""
    n = max(0, int(n))
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        thousands = n / 1_000.0
        # whole-thousands and big values read fine as ints; small fractions keep one decimal
        if n % 1_000 == 0 or n >= 10_000:
            return f"{thousands:.0f}k"
        return f"{thousands:.1f}k"
    return f"{n / 1_000_000.0:.2f}M"


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #
@dataclass
class GuardResult:
    """The fail-closed verdict of a single guard."""

    name: str
    passed: bool
    reason: str = ""
    details: dict = field(default_factory=dict)

    @classmethod
    def ok(cls, name: str, reason: str = "") -> GuardResult:
        return cls(name=name, passed=True, reason=reason)

    @classmethod
    def fail(cls, name: str, reason: str, **details: object) -> GuardResult:
        return cls(name=name, passed=False, reason=reason, details=dict(details))


# --------------------------------------------------------------------------- #
# Implementation (implementer agent output)
# --------------------------------------------------------------------------- #
@dataclass
class ImplementationResult:
    """What the implementer produced on its isolated branch."""

    branch: str
    diff: str = ""
    files_changed: list[str] = field(default_factory=list)
    iterations: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    notes: str = ""
    no_progress: bool = False  # set when the no-progress detector bailed
    error: str = ""

    @property
    def empty_diff(self) -> bool:
        return not self.diff.strip()


# --------------------------------------------------------------------------- #
# Review (reviewer agent output)
# --------------------------------------------------------------------------- #
@dataclass
class ReviewComment:
    body: str
    path: str | None = None
    line: int | None = None


@dataclass
class ReviewVerdict:
    decision: Decision
    summary: str = ""
    comments: list[ReviewComment] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        return self.decision == Decision.APPROVE

    @classmethod
    def from_dict(cls, data: dict) -> ReviewVerdict:
        raw = str(data.get("decision", "request_changes")).strip().lower()
        decision = Decision.APPROVE if raw == "approve" else Decision.REQUEST_CHANGES
        comments = [
            ReviewComment(
                body=str(c.get("body", "")),
                path=c.get("path"),
                line=c.get("line"),
            )
            for c in data.get("comments", [])
            if isinstance(c, dict)
        ]
        return cls(decision=decision, summary=str(data.get("summary", "")), comments=comments)


# --------------------------------------------------------------------------- #
# Cost log
# --------------------------------------------------------------------------- #
def utcnow_iso() -> str:
    """UTC timestamp in ISO-8601 (seconds precision)."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass
class RunRecord:
    """One row of cost_log.jsonl — audit trail and v2 training data."""

    ticket_id: int
    title: str
    features: dict
    estimated_cost: float
    estimated_iterations: float
    actual_cost: float
    actual_iterations: int
    outcome: str
    timestamp: str = field(default_factory=utcnow_iso)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, line: str) -> RunRecord:
        data = json.loads(line)
        return cls(**data)
