"""Tests for the provider-agnostic LLM port (sis.llm). No network, no keys."""

import pytest

from sis import llm, proposer


class _FakeClient:
    """Stands in for a real provider client — records the prompt, returns canned text."""

    model = "fake-model-1"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def complete(self, *, system: str, user: str, max_tokens: int) -> llm.LLMResponse:
        self.calls.append((system, user, max_tokens))
        return llm.LLMResponse(
            text="```python\ndef f(x: int) -> int:\n    return x\n```",
            cost_usd=0.02, model=self.model)


def test_default_provider_is_anthropic(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("SIS_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("SIS_LLM_MODEL", raising=False)
    client = llm.get_llm_client()
    assert isinstance(client, llm.AnthropicClient)
    assert client.model == llm.DEFAULT_ANTHROPIC_MODEL


def test_model_override(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SIS_LLM_MODEL", "claude-sonnet-5")
    assert llm.get_llm_client().model == "claude-sonnet-5"
    assert llm.configured_model() == "claude-sonnet-5"  # no API call


def test_unknown_provider_fails_loudly(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SIS_LLM_PROVIDER", "acme")
    with pytest.raises(ValueError, match="SIS_LLM_PROVIDER"):
        llm.get_llm_client()


def test_proposer_uses_the_configured_client(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # The proposer is vendor-agnostic: it calls whatever sis.llm hands back, and
    # records that call's cost + model for the CEO brakes / episodic log.
    monkeypatch.setenv("SIS_PROPOSER", "claude")
    fake = _FakeClient()
    monkeypatch.setattr(llm, "get_llm_client", lambda *a, **k: fake)

    out = proposer.propose("CURRENT SOURCE", 0.5)

    assert out == "def f(x: int) -> int:\n    return x\n"   # fences stripped
    assert proposer.last_cost_usd() == 0.02
    assert proposer.last_model() == "fake-model-1"
    assert "CURRENT SOURCE" in fake.calls[0][1]              # source went into the prompt
