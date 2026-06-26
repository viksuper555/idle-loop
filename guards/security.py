"""Security guard — defensive secret/injection scanner for diffs.

Scans only the *added* lines of a unified diff (lines starting with ``+`` but
excluding the ``+++`` file header) for leaked secrets and obvious dangerous
code patterns. This is a defensive detector only: it flags suspicious additions
so a human can intervene; it never modifies code.

Like every guard it fails closed — any doubt (including an unexpected error in
the scanner) yields a failing :class:`GuardResult` via :func:`run_guard`.
"""

from __future__ import annotations

import re

from config import Config
from guards.base import GuardContext
from models import GuardResult

# Cap on the number of findings surfaced in the result details, to keep PR
# comments and logs readable when a diff is pathologically dirty.
_MAX_FINDINGS = 20

# Values that look like a secret assignment but are obviously placeholders.
_PLACEHOLDER_MARKERS = (
    "example",
    "changeme",
    "xxxx",
    "<",
    "your_",
    "dummy",
    "test",
    "fake",
    "placeholder",
    "redacted",
)

# --------------------------------------------------------------------------- #
# Detectors
# --------------------------------------------------------------------------- #
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"
)
_AWS_KEY = re.compile(r"AKIA[0-9A-Z]{16}")
_GITHUB_TOKEN = re.compile(r"ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}")
_SLACK_TOKEN = re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")
_GENERIC_SECRET = re.compile(
    r"(?i)(?:secret|token|api[_-]?key|password|passwd|client[_-]?secret)"
    r"\s*[:=]\s*['\"]([^'\"]{12,})['\"]"
)

# Injection patterns.
_SHELL_TRUE = re.compile(r"\bshell\s*=\s*True\b")
_OS_SYSTEM_DYNAMIC = re.compile(
    r"os\.system\(\s*(?:f['\"]|[^)]*?(?:\.format\(|%\s|\+))"
)
_EVAL_EXEC_TAINTED = re.compile(
    r"\b(?:eval|exec)\([^)]*\b(?:request|input|argv)\b"
)
_PICKLE_LOADS = re.compile(r"\bpickle\.loads\(")
_YAML_LOAD = re.compile(r"\byaml\.load\(")
_YAML_SAFELOADER = re.compile(r"SafeLoader")

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key", _PRIVATE_KEY),
    ("aws_access_key_id", _AWS_KEY),
    ("github_token", _GITHUB_TOKEN),
    ("slack_token", _SLACK_TOKEN),
]


def _is_placeholder(value: str) -> bool:
    """True if a captured secret value is an obvious placeholder."""
    low = value.lower()
    return any(marker in low for marker in _PLACEHOLDER_MARKERS)


def _added_lines(diff: str) -> list[tuple[int, str]]:
    """Yield ``(line_no, content)`` for added lines only.

    Added lines start with a single ``+`` and exclude the ``+++`` header.
    ``line_no`` is the 1-based index of the line within the raw diff text, which
    is enough for a human to locate the finding.
    """
    out: list[tuple[int, str]] = []
    for idx, raw in enumerate(diff.splitlines(), start=1):
        if not raw.startswith("+"):
            continue
        if raw.startswith("+++"):
            continue
        out.append((idx, raw[1:]))
    return out


def scan_diff(diff: str) -> list[dict]:
    """Scan a unified diff's added lines for secrets/injection patterns.

    Returns a list of findings, each a dict with ``pattern``, ``line_no``, and
    ``snippet`` keys. Only added lines (``+`` prefix, not ``+++``) are scanned;
    removed and context lines are ignored. Obvious placeholder secret values are
    skipped.
    """
    findings: list[dict] = []
    for line_no, content in _added_lines(diff):
        snippet = content.strip()[:200]

        for name, pattern in _PATTERNS:
            if pattern.search(content):
                findings.append(
                    {"pattern": name, "line_no": line_no, "snippet": snippet}
                )

        m = _GENERIC_SECRET.search(content)
        if m and not _is_placeholder(m.group(1)):
            findings.append(
                {"pattern": "generic_secret", "line_no": line_no, "snippet": snippet}
            )

        if _SHELL_TRUE.search(content):
            findings.append(
                {"pattern": "shell_true", "line_no": line_no, "snippet": snippet}
            )
        if _OS_SYSTEM_DYNAMIC.search(content):
            findings.append(
                {"pattern": "os_system_dynamic", "line_no": line_no, "snippet": snippet}
            )
        if _EVAL_EXEC_TAINTED.search(content):
            findings.append(
                {"pattern": "eval_exec_tainted", "line_no": line_no, "snippet": snippet}
            )
        if _PICKLE_LOADS.search(content):
            findings.append(
                {"pattern": "pickle_loads", "line_no": line_no, "snippet": snippet}
            )
        if _YAML_LOAD.search(content) and not _YAML_SAFELOADER.search(content):
            findings.append(
                {"pattern": "yaml_load_unsafe", "line_no": line_no, "snippet": snippet}
            )

    return findings


class SecurityGuard:
    """Fail-closed guard that flags leaked secrets and injection patterns."""

    name = "security"

    def __init__(self, config: Config) -> None:
        self.config = config

    def check(self, ctx: GuardContext) -> GuardResult:
        """Pass when the diff's added lines contain no findings."""
        findings = scan_diff(ctx.diff)
        if findings:
            capped = findings[:_MAX_FINDINGS]
            return GuardResult.fail(
                self.name,
                f"potential secret/injection: {len(findings)} findings",
                findings=capped,
            )
        return GuardResult.ok(self.name, "no secret/injection patterns detected")
