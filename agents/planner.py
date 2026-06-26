"""The planner agent — a cheap, read-only planning pass that yields a token budget.

Runs ``claude -p`` headlessly INSIDE the ticket's worktree with only read tools
(Read/Grep/Glob), so it inspects the real code, then returns a JSON estimate of
the input/output tokens the implementation will cost. Its claude session id is
saved to the worktree session file so the implementer RESUMES this very session —
the plan carries straight into implementation, deterministically.

Like the reviewer it fails soft: an unparseable or invalid reply yields ``None``
so the orchestrator falls back to the heuristic estimator. ``HarnessRateLimited``
propagates so a limit during planning still stops the loop.

No API key — auth is the Claude Code login.
"""

from __future__ import annotations

from dataclasses import dataclass

from agents.harness import ClaudeHarness
from agents.implementer import save_session
from agents.reviewer import _extract_json
from config import Config
from models import Ticket

# Read-only: the planner inspects code but must never mutate it, and we keep it
# off Bash so a planning pass can't run anything in the live worktree.
_READ_ONLY_DISALLOWED = ["Edit", "Write", "NotebookEdit", "Bash"]

_PROMPT = """You are a planning agent pricing one GitHub ticket for an autonomous \
coding agent (Claude Code). Inspect the codebase in your working directory with the \
read-only tools available (Read/Grep/Glob) — do NOT edit anything — then produce a \
short implementation plan and ESTIMATE the tokens the implementation will spend \
end-to-end, INCLUDING writing/updating tests and iterating until they pass.

Think concretely: files to read for context, files to create or modify, test files, \
and the back-and-forth of running tests and fixing failures. Be realistic, not \
optimistic — real implementation costs more than the happy path. An implementer will \
RESUME this same session to do the work, so your exploration is not wasted.

--- TICKET #{number}: {title} ---
{body}

Acceptance criteria:
{criteria}

Respond with ONLY a single JSON object (no prose, no markdown fences) of the form:
{{"estimated_input_tokens": <int>, "estimated_output_tokens": <int>, \
"plan": "<2-4 sentence plan>", "rationale": "<one sentence on the token figure>"}}"""


@dataclass
class PlanResult:
    """A deterministic token budget from a planning pass (plus its own cost)."""

    estimated_input_tokens: int
    estimated_output_tokens: int
    plan_text: str = ""
    session_id: str = ""
    cost_usd: float = 0.0  # what the planning pass itself cost
    input_tokens: int = 0  # tokens the planning pass itself consumed
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.estimated_input_tokens + self.estimated_output_tokens


class Planner:
    """Prices a ticket via a cheap read-only claude session, deterministically."""

    def __init__(self, config: Config, harness: ClaudeHarness | None = None) -> None:
        self.config = config
        self.harness = harness or ClaudeHarness(config)

    def _build_prompt(self, ticket: Ticket) -> str:
        criteria = ticket.acceptance_criteria or []
        ac = "\n".join(f"- {c}" for c in criteria) if criteria else "(none listed)"
        return _PROMPT.format(
            number=ticket.number, title=ticket.title, body=ticket.body, criteria=ac
        )

    def plan(self, ticket: Ticket, repo_dir: str) -> PlanResult | None:
        """Return a deterministic token budget, or ``None`` to fall back.

        Runs in ``repo_dir`` so it reads the real code, seeds the worktree session
        for the implementer to resume, and validates the JSON token budget. Lets
        :class:`HarnessRateLimited` propagate (do not swallow it).
        """
        result = self.harness.run(
            self._build_prompt(ticket),
            cwd=repo_dir,
            model=self.config.planner.model or self.config.model,
            disallowed_tools=_READ_ONLY_DISALLOWED,
            timeout_s=self.config.planner.timeout_s,
        )
        # Seed continuity even if parsing fails — the planning conversation is still
        # useful context for the implementer's resume.
        save_session(repo_dir, result.session_id)

        data = _extract_json(result.text)
        if not data:
            return None
        try:
            est_in = int(data["estimated_input_tokens"])
            est_out = int(data["estimated_output_tokens"])
        except (KeyError, TypeError, ValueError):
            return None
        if est_in < 0 or est_out < 0 or (est_in + est_out) == 0:
            return None

        return PlanResult(
            estimated_input_tokens=est_in,
            estimated_output_tokens=est_out,
            plan_text=str(data.get("plan", "")),
            session_id=result.session_id,
            cost_usd=result.cost_usd,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )


__all__ = ["Planner", "PlanResult"]
