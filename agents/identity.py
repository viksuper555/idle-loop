"""Per-agent GitHub identities — so each agent comments under its own username.

Every comment idle-loop posts today goes through one GitHub token, so the
planner, reviewer, implementer, and the loop itself all show up as the same
user — confusing when reading a thread. This routes each *logical agent* to its
own credential: a GitHub App installation token (or a per-agent bot PAT) that a
maintainer provisions and exposes via an environment variable.

Provisioning the apps / tokens is a human step (secrets), so this stays
dependency-free and fails safe: when an agent has no configured token the router
hands back the shared default client, i.e. exactly today's behaviour. The
identities only diverge once a maintainer opts in (``identities.enabled`` + the
env vars set).
"""

from __future__ import annotations

import os
from collections.abc import Callable

# Logical agents that post to GitHub. Each maps to its own identity when one is
# configured; otherwise all share the default client.
PLANNER = "planner"
IMPLEMENTER = "implementer"
REVIEWER = "reviewer"
LOOP = "loop"  # the orchestrator itself (parks, merges, guard verdicts)
AGENTS: tuple[str, ...] = (PLANNER, IMPLEMENTER, REVIEWER, LOOP)


class IdentityRouter:
    """Maps a logical agent to the GitHub client that posts under its identity.

    With no per-agent clients configured, :meth:`client` always returns the
    shared default — behaviour is unchanged until a maintainer provisions tokens.
    """

    def __init__(self, default_client, per_agent: dict | None = None) -> None:
        self.default = default_client
        self._per_agent = dict(per_agent or {})

    def client(self, agent: str | None = None):
        """The client for ``agent`` — its own identity if configured, else default."""
        if agent is None:
            return self.default
        return self._per_agent.get(agent, self.default)

    def configured_agents(self) -> set[str]:
        """Agents that resolved to a distinct (non-default) identity."""
        return set(self._per_agent)

    @classmethod
    def from_config(
        cls, config, default_client, client_factory: Callable
    ) -> IdentityRouter:
        """Build a router from ``config.identities``.

        For each agent whose configured env var holds a token, build a client
        with ``client_factory(config.repo, token=...)``; agents with no token
        fall back to ``default_client``. A no-op (all-default) router when
        ``identities.enabled`` is false.
        """
        per_agent: dict = {}
        identities = getattr(config, "identities", None)
        if identities is not None and identities.enabled:
            for agent, env_var in identities.tokens.items():
                token = os.environ.get(env_var)
                if token:
                    per_agent[agent] = client_factory(config.repo, token=token)
        return cls(default_client, per_agent)


__all__ = ["AGENTS", "PLANNER", "IMPLEMENTER", "REVIEWER", "LOOP", "IdentityRouter"]
