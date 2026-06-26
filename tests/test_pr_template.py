"""Tests for pr_template.py — the maintainable PR body template."""

from __future__ import annotations

import pr_template
from pr_template import PRContent, render_pr_body


def _content(**over) -> PRContent:
    base = dict(
        issue_number=7,
        title="Add a thing",
        branch="feature/issue-7",
        issue_url="https://gh/issues/7",
        acceptance_criteria=["does X", "does Y"],
        files_changed=3,
        iterations=2,
        cost_badges_md="![cost](https://img.shields.io/badge/x)",
        gates_md="**Gates:**\n- ✅ `scope` — passed",
        commentary="Implemented X and Y; added tests.",
    )
    base.update(over)
    return PRContent(**base)


def test_render_includes_all_expected_parts():
    body = render_pr_body(_content())
    assert "Closes #7" in body
    assert "[#7](https://gh/issues/7)" in body  # link to the summary
    assert "`feature/issue-7`" in body
    # AC rendered as a checklist.
    assert "- [ ] does X" in body and "- [ ] does Y" in body
    # Cost chips + gates.
    assert "img.shields.io" in body
    assert "Gates:" in body
    # Commentary + PLG footer.
    assert "Implemented X and Y" in body
    assert pr_template.FOOTER in body


def test_section_order_summary_first_then_ac_then_cost():
    body = render_pr_body(_content())
    assert body.index("Closes #7") < body.index("Acceptance criteria")
    assert body.index("Acceptance criteria") < body.index("### Cost")


def test_empty_sections_are_omitted():
    body = render_pr_body(
        _content(acceptance_criteria=[], commentary="  ", cost_badges_md="")
    )
    assert "Acceptance criteria" not in body
    assert "### Cost" not in body
    assert "### Commentary" not in body
    # Footer always present.
    assert pr_template.FOOTER in body


def test_template_is_extensible_via_sections(monkeypatch):
    # Adding a section is just appending a callable — it shows up in the body.
    def extra(_c):
        return "### Extra\nhello from a new section"

    monkeypatch.setattr(pr_template, "SECTIONS", [*pr_template.SECTIONS, extra])
    body = render_pr_body(_content())
    assert "hello from a new section" in body
    # And it lands before the footer.
    assert body.index("hello from a new section") < body.index(pr_template.FOOTER)
