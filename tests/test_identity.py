"""Tests for agents.identity.IdentityRouter and its config wiring."""

from __future__ import annotations

from agents.identity import (
    LOOP,
    PLANNER,
    REVIEWER,
    IdentityRouter,
)
from config import Config


class FakeClient:
    def __init__(self, repo, token=None):
        self.repo = repo
        self.token = token


def test_router_returns_default_when_nothing_configured():
    default = object()
    router = IdentityRouter(default)
    assert router.client() is default
    assert router.client(PLANNER) is default  # unconfigured -> default
    assert router.configured_agents() == set()


def test_router_routes_a_configured_agent_only():
    default = object()
    planner = object()
    router = IdentityRouter(default, {PLANNER: planner})
    assert router.client(PLANNER) is planner
    assert router.client(REVIEWER) is default  # not configured -> default
    assert router.configured_agents() == {PLANNER}


def test_from_config_disabled_is_all_default():
    cfg = Config(repo="o/n")  # identities.enabled defaults False
    default = object()
    router = IdentityRouter.from_config(cfg, default, FakeClient)
    assert router.configured_agents() == set()
    assert router.client(PLANNER) is default


def test_from_config_builds_a_client_per_set_token(monkeypatch):
    cfg = Config(repo="o/n")
    cfg.identities.enabled = True
    cfg.identities.tokens = {
        "planner": "ENV_P",
        "reviewer": "ENV_R",
        "loop": "ENV_L",  # left unset -> falls back to default
    }
    monkeypatch.setenv("ENV_P", "tok-planner")
    monkeypatch.setenv("ENV_R", "tok-reviewer")
    monkeypatch.delenv("ENV_L", raising=False)

    default = FakeClient("o/n", token="default")
    router = IdentityRouter.from_config(cfg, default, FakeClient)

    assert router.configured_agents() == {"planner", "reviewer"}
    assert router.client(PLANNER).token == "tok-planner"
    assert router.client(REVIEWER).token == "tok-reviewer"
    assert router.client(LOOP) is default  # unset env var -> shared default


def test_config_loads_identities_block(tmp_path):
    from config import load_config

    cfg_file = tmp_path / "idle.config.yaml"
    cfg_file.write_text(
        "repo: o/n\n"
        "identities:\n"
        "  enabled: true\n"
        "  tokens:\n"
        "    planner: MY_PLANNER_TOKEN\n",
        encoding="utf-8",
    )
    cfg = load_config(str(cfg_file))
    assert cfg.identities.enabled is True
    assert cfg.identities.tokens == {"planner": "MY_PLANNER_TOKEN"}
