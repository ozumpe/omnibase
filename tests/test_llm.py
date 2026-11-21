"""Tests for the provider-agnostic LLM port (sis.llm). No network, no keys."""

import pytest

from sis import cost, llm, proposer


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


def test_an_unpriced_model_is_refused_before_any_call(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # L25 (OMNI-71): the client is built only to make a call, so refusing here
    # means an unpriced model never spends a dollar the cap cannot see.
    monkeypatch.setenv("SIS_LLM_MODEL", "claude-opus-9")
    with pytest.raises(cost.UnpricedModel, match="claude-opus-9"):
        llm.get_llm_client()


def test_a_real_proposer_with_an_unpriced_model_is_refused_up_front(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SIS_LLM_MODEL", "claude-opus-9")
    monkeypatch.setenv("SIS_PROPOSER", "claude")
    with pytest.raises(cost.UnpricedModel, match="proposer.llm_model"):
        llm.ensure_proposer_priced()
    monkeypatch.setenv("SIS_PROPOSER", "stub")
    llm.ensure_proposer_priced()   # the stub calls no model, so nothing to price
    monkeypatch.setenv("SIS_PROPOSER", "claude")
    monkeypatch.setenv("SIS_LLM_MODEL", "claude-opus-5-5")
    llm.ensure_proposer_priced()


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


def test_the_anthropic_client_says_when_the_answer_was_cut_off(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # L33 (OMNI-78): stop_reason "max_tokens" means the text is not the whole answer.
    import sys
    from types import SimpleNamespace

    def fake_create(**kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="def f(")],
            usage=SimpleNamespace(input_tokens=10, output_tokens=8000,
                                  cache_creation_input_tokens=0, cache_read_input_tokens=0),
            stop_reason=kwargs["_stop"],
        )

    for stop, cut in (("max_tokens", True), ("end_turn", False)):
        fake = SimpleNamespace(Anthropic=lambda stop=stop: SimpleNamespace(
            messages=SimpleNamespace(create=lambda **kw: fake_create(**kw, _stop=stop))))
        monkeypatch.setitem(sys.modules, "anthropic", fake)
        response = llm.AnthropicClient().complete(system="s", user="u", max_tokens=8000)
        assert response.truncated is cut, stop


def test_the_anthropic_client_reports_its_token_counts(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # OMNI-220 (L76): only the cost came back, so a cut-off could not be told
    # from a long answer in the logs.
    import sys
    from types import SimpleNamespace

    message = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="def f(): ...")],
        usage=SimpleNamespace(input_tokens=5210, output_tokens=7570,
                              cache_creation_input_tokens=1200, cache_read_input_tokens=None),
        stop_reason="end_turn")
    fake = SimpleNamespace(Anthropic=lambda: SimpleNamespace(
        messages=SimpleNamespace(create=lambda **kw: message)))
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    response = llm.AnthropicClient().complete(system="s", user="u", max_tokens=8000)
    assert response.usage == llm.TokenUsage(input_tokens=5210, output_tokens=7570,
                                            cache_creation_tokens=1200, cache_read_tokens=0)
    assert response.stop_reason == "end_turn"


def test_a_call_over_the_sdks_non_streaming_limit_is_streamed(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # OMNI-219: the SDK refuses a non-streaming call much above 21000 output
    # tokens, and the retry of a cut-off proposal asks for more. The first
    # call is sent as before.
    import sys
    from types import SimpleNamespace

    message = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="def f(): ...")],
        usage=SimpleNamespace(input_tokens=10, output_tokens=25000,
                              cache_creation_input_tokens=0, cache_read_input_tokens=0),
        stop_reason="end_turn")
    how: list[tuple[str, int]] = []

    class _Stream:
        def __enter__(self) -> "_Stream":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def get_final_message(self) -> SimpleNamespace:
            return message

    def create(**kw):  # type: ignore[no-untyped-def]
        how.append(("create", kw["max_tokens"]))
        return message

    def stream(**kw):  # type: ignore[no-untyped-def]
        how.append(("stream", kw["max_tokens"]))
        return _Stream()

    fake = SimpleNamespace(Anthropic=lambda: SimpleNamespace(
        messages=SimpleNamespace(create=create, stream=stream)))
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    for limit in (16000, 32000):
        response = llm.AnthropicClient().complete(system="s", user="u", max_tokens=limit)
        assert response.text == "def f(): ..." and not response.truncated
    assert how == [("create", 16000), ("stream", 32000)]


def test_a_tool_turn_is_sent_back_as_the_sdk_returned_it(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # OMNI-206: the thinking before a tool call must go back with it, and every
    # turn's cost comes from that turn's usage. Blocks shaped like the SDK's
    # (a type and model_dump), so the test needs no anthropic package.
    import sys
    from types import SimpleNamespace

    def block(**fields: object) -> SimpleNamespace:
        return SimpleNamespace(**fields, model_dump=lambda exclude_none=True: dict(fields))

    sent: dict[str, object] = {}
    message = SimpleNamespace(
        content=[block(type="thinking", thinking="fit it", signature="sig"),
                 block(type="text", text="running"),
                 block(type="tool_use", id="t1", name="run_python", input={"code": "print(1)"})],
        stop_reason="tool_use",
        usage=SimpleNamespace(input_tokens=1000, output_tokens=200,
                              cache_creation_input_tokens=0, cache_read_input_tokens=0))

    def create(**kwargs: object) -> SimpleNamespace:
        sent.update(kwargs)
        return message

    fake = SimpleNamespace(Anthropic=lambda: SimpleNamespace(
        messages=SimpleNamespace(create=create)))
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    turn = llm.AnthropicClient("claude-opus-4-8").converse(
        system="s", messages=[{"role": "user", "content": "go"}],
        tools=[{"name": "run_python"}], max_tokens=8000, allow_tools=False)

    assert turn.calls == (llm.ToolCall("t1", "run_python", {"code": "print(1)"}),)
    assert [b["type"] for b in turn.content] == ["thinking", "text", "tool_use"]
    assert turn.content[0]["signature"] == "sig"
    assert turn.response.cost_usd > 0 and turn.response.text == "running"
    assert sent["tool_choice"] == {"type": "none"}
    first = sent["messages"][0]  # type: ignore[index]
    assert first["content"][-1]["cache_control"] == {"type": "ephemeral"}
