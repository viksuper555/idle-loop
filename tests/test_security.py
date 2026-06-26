"""Tests for guards.security — SecurityGuard + scan_diff.

All secrets here are SYNTHETIC (e.g. "AKIA" + "A"*16). No real credentials.
"""

from __future__ import annotations

from config import Config
from guards.base import GuardContext
from guards.security import SecurityGuard, scan_diff
from models import Ticket


def _ctx(diff: str) -> GuardContext:
    cfg = Config(repo="owner/name")
    ticket = Ticket(number=1, title="t", body="b")
    return GuardContext(ticket=ticket, config=cfg, diff=diff)


def _guard() -> SecurityGuard:
    return SecurityGuard(Config(repo="owner/name"))


# --------------------------------------------------------------------------- #
# Secret detectors
# --------------------------------------------------------------------------- #
def test_aws_access_key_flagged() -> None:
    diff = "+aws_id = '" + "AKIA" + "A" * 16 + "'\n"
    findings = scan_diff(diff)
    assert any(f["pattern"] == "aws_access_key_id" for f in findings)


def test_private_key_flagged() -> None:
    diff = "+-----BEGIN PRIVATE KEY-----\n"
    findings = scan_diff(diff)
    assert any(f["pattern"] == "private_key" for f in findings)


def test_rsa_private_key_flagged() -> None:
    diff = "+-----BEGIN RSA PRIVATE KEY-----\n"
    assert any(f["pattern"] == "private_key" for f in scan_diff(diff))


def test_github_token_flagged() -> None:
    diff = "+token = 'ghp_" + "a" * 36 + "'\n"
    assert any(f["pattern"] == "github_token" for f in scan_diff(diff))


def test_github_pat_flagged() -> None:
    diff = "+t = 'github_pat_" + "a" * 40 + "'\n"
    assert any(f["pattern"] == "github_token" for f in scan_diff(diff))


def test_slack_token_flagged() -> None:
    diff = "+slack = 'xoxb-" + "1234567890" + "-abcdef'\n"
    assert any(f["pattern"] == "slack_token" for f in scan_diff(diff))


def test_generic_secret_flagged() -> None:
    diff = "+password = 'sup3rs3cr3tvalue'\n"
    assert any(f["pattern"] == "generic_secret" for f in scan_diff(diff))


# --------------------------------------------------------------------------- #
# Placeholders pass
# --------------------------------------------------------------------------- #
def test_placeholder_value_passes() -> None:
    diff = "+api_key = 'your_key_here'\n"
    assert scan_diff(diff) == []
    assert _guard().check(_ctx(diff)).passed


def test_changeme_placeholder_passes() -> None:
    diff = "+secret = 'changeme_now_please'\n"
    assert scan_diff(diff) == []


def test_short_value_not_flagged() -> None:
    # Under 12 chars -> not a generic secret.
    diff = "+password = 'short'\n"
    assert scan_diff(diff) == []


# --------------------------------------------------------------------------- #
# Injection detectors
# --------------------------------------------------------------------------- #
def test_shell_true_flagged() -> None:
    diff = "+subprocess.run(f'ls {path}', shell=True)\n"
    assert any(f["pattern"] == "shell_true" for f in scan_diff(diff))


def test_os_system_fstring_flagged() -> None:
    diff = "+os.system(f'rm {target}')\n"
    assert any(f["pattern"] == "os_system_dynamic" for f in scan_diff(diff))


def test_os_system_concat_flagged() -> None:
    diff = "+os.system('rm ' + target)\n"
    assert any(f["pattern"] == "os_system_dynamic" for f in scan_diff(diff))


def test_eval_tainted_flagged() -> None:
    diff = "+result = eval(request.args.get('x'))\n"
    assert any(f["pattern"] == "eval_exec_tainted" for f in scan_diff(diff))


def test_exec_input_flagged() -> None:
    diff = "+exec(input())\n"
    assert any(f["pattern"] == "eval_exec_tainted" for f in scan_diff(diff))


def test_pickle_loads_flagged() -> None:
    diff = "+obj = pickle.loads(data)\n"
    assert any(f["pattern"] == "pickle_loads" for f in scan_diff(diff))


def test_yaml_load_unsafe_flagged() -> None:
    diff = "+cfg = yaml.load(text)\n"
    assert any(f["pattern"] == "yaml_load_unsafe" for f in scan_diff(diff))


def test_yaml_load_safeloader_passes() -> None:
    diff = "+cfg = yaml.load(text, Loader=yaml.SafeLoader)\n"
    assert not any(f["pattern"] == "yaml_load_unsafe" for f in scan_diff(diff))


# --------------------------------------------------------------------------- #
# Only added lines are scanned
# --------------------------------------------------------------------------- #
def test_removed_lines_not_scanned() -> None:
    diff = "-aws_id = '" + "AKIA" + "A" * 16 + "'\n"
    assert scan_diff(diff) == []


def test_context_lines_not_scanned() -> None:
    diff = " aws_id = '" + "AKIA" + "A" * 16 + "'\n"
    assert scan_diff(diff) == []


def test_diff_header_not_scanned() -> None:
    # The +++ file header must be ignored even though it starts with '+'.
    secret = "AKIA" + "A" * 16
    diff = f"+++ b/{secret}.py\n"
    assert scan_diff(diff) == []


# --------------------------------------------------------------------------- #
# Guard wrapper behavior
# --------------------------------------------------------------------------- #
def test_guard_name() -> None:
    assert _guard().name == "security"


def test_guard_fails_on_finding() -> None:
    diff = "+key = '" + "AKIA" + "A" * 16 + "'\n"
    result = _guard().check(_ctx(diff))
    assert not result.passed
    assert "findings" in result.details
    assert len(result.details["findings"]) >= 1


def test_guard_passes_on_clean_diff() -> None:
    diff = "+def add(a, b):\n+    return a + b\n"
    result = _guard().check(_ctx(diff))
    assert result.passed


def test_findings_capped() -> None:
    line = "+key = '" + "AKIA" + "A" * 16 + "'\n"
    diff = line * 50
    result = _guard().check(_ctx(diff))
    assert not result.passed
    assert len(result.details["findings"]) <= 20


def test_finding_shape() -> None:
    diff = "+key = '" + "AKIA" + "A" * 16 + "'\n"
    findings = scan_diff(diff)
    f = findings[0]
    assert set(f.keys()) == {"pattern", "line_no", "snippet"}
    assert isinstance(f["line_no"], int)
