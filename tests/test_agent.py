import asyncio

from polyweather import agent


def _clear(monkeypatch):
    for k in ("OPENAI_API_KEY", "POLY_AGENT_MODEL", "POLY_AGENT_MODEL_RETRY"):
        monkeypatch.delenv(k, raising=False)


def test_disabled_without_key(monkeypatch):
    _clear(monkeypatch)
    assert agent.enabled() is False
    # propose() short-circuits to None before touching the client → deterministic fallback
    assert asyncio.run(agent.propose(None, {"city": "London"})) is None


def test_enabled_with_key(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")
    assert agent.enabled() is True


def test_model_defaults_and_override(monkeypatch):
    _clear(monkeypatch)
    assert agent._model() == "gpt-5.4-mini"              # PASS-1 default
    assert agent.retry_model() == "gpt-5.4"              # PASS-2 default (stronger)
    monkeypatch.setenv("POLY_AGENT_MODEL", "gpt-5.4")
    monkeypatch.setenv("POLY_AGENT_MODEL_RETRY", "gpt-5.4-turbo")
    assert agent._model() == "gpt-5.4" and agent.retry_model() == "gpt-5.4-turbo"   # overrides honored


def test_stale_claude_model_name_ignored(monkeypatch):
    # a leftover Anthropic model name can't break the OpenAI call → falls back to the default
    _clear(monkeypatch)
    monkeypatch.setenv("POLY_AGENT_MODEL", "claude-opus-4-8")
    monkeypatch.setenv("POLY_AGENT_MODEL_RETRY", "claude-x")
    assert agent._model() == "gpt-5.4-mini" and agent.retry_model() == "gpt-5.4"


def test_decision_tool_schema_is_constrained():
    s = agent.SCHEMA["properties"]
    assert s["action"]["enum"] == ["BET", "ESCALATE", "SKIP"]
    assert s["comparator"]["enum"] == ["<=", "==", ">="]
    assert "threshold" in s and "reason" in s
    assert agent.OPENAI_TOOL["function"]["name"] == "emit_decision"
    assert agent.OPENAI_TOOL["function"]["parameters"] is agent.SCHEMA


def test_propose_strike_disabled_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert asyncio.run(agent.propose_strike(None, {"city": "London"})) is None   # no key → caller falls back


def test_strike_schema_constrained():
    assert agent.STRIKE_SCHEMA["required"] == ["strike_utc", "reason"]
    assert agent.STRIKE_TOOL["function"]["name"] == "emit_strike"
    assert agent.STRIKE_TOOL["function"]["parameters"] is agent.STRIKE_SCHEMA
