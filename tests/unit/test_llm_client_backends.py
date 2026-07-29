"""LLM backend selection + the subscription client's SDK bridge (no network)."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass

import pytest

from pipeline.agents.client import AnthropicClient, SubscriptionClient, default_client


def _stub_sdk(monkeypatch, messages):
    """Install a fake claude_agent_sdk yielding the given message objects."""
    mod = types.ModuleType("claude_agent_sdk")

    class ClaudeAgentOptions:  # accepts the kwargs we pass; records them
        def __init__(self, **kw):
            self.kw = kw

    async def query(*, prompt, options):
        query.captured = {"prompt": prompt, "options": options}
        for m in messages:
            yield m

    mod.ClaudeAgentOptions = ClaudeAgentOptions
    mod.query = query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    return mod


@dataclass
class _Block:
    text: str


def test_default_client_backend_selection(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-key")
    monkeypatch.delenv("CLAUDE_API", raising=False)
    assert isinstance(default_client(), AnthropicClient)  # unset -> API (safe default)
    monkeypatch.setenv("CLAUDE_API", "true")
    assert isinstance(default_client(), AnthropicClient)

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "dummy-oauth")
    _stub_sdk(monkeypatch, [])
    for off in ("false", "FALSE", "0", "no", "off"):
        monkeypatch.setenv("CLAUDE_API", off)
        assert isinstance(default_client(), SubscriptionClient)


def test_subscription_requires_token(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="setup-token"):
        SubscriptionClient()


def test_subscription_complete_collects_text_and_usage(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "dummy-oauth")
    assistant = types.SimpleNamespace(content=[_Block('{"rankings"'), _Block(": []}")])
    result = types.SimpleNamespace(
        content=None,
        result=None,
        usage={"input_tokens": 120, "output_tokens": 40, "cache_read_input_tokens": 7},
    )
    mod = _stub_sdk(monkeypatch, [assistant, result])

    out = SubscriptionClient().complete(
        system="sys prompt", user="user prompt", model="sonnet-5"
    )
    assert out.text == '{"rankings": []}'
    assert out.model == "claude-sonnet-5"  # alias resolved like the API client
    assert (out.input_tokens, out.output_tokens, out.cache_read_tokens) == (120, 40, 7)
    assert out.cost_usd == 0.0  # subscription backend: plan limits, not dollars
    opts = mod.query.captured["options"].kw
    assert opts["allowed_tools"] == [] and opts["max_turns"] == 1
    assert opts["system_prompt"] == "sys prompt"


def test_subscription_falls_back_to_result_text(monkeypatch):
    """No assistant content blocks (SDK shape drift) -> ResultMessage.result wins."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "dummy-oauth")
    only_result = types.SimpleNamespace(content=None, result='{"ok": true}', usage=None)
    _stub_sdk(monkeypatch, [only_result])
    out = SubscriptionClient().complete(system="s", user="u", model="haiku-4-5")
    assert out.text == '{"ok": true}'
    assert out.input_tokens == 0 and out.cost_usd == 0.0
