"""sis.llm — a provider-agnostic LLM client (port + adapters).

The proposer builds the prompt and extracts the code; *which* model answers is
chosen here, behind a small port, so the loop never depends on one vendor.

Selected by ``SIS_LLM_PROVIDER`` (default ``anthropic``); ``SIS_LLM_MODEL``
overrides the model within a provider. Provider SDKs are optional and imported
lazily, so the default in-memory/stub path needs none of them.

**Adding a provider** is a small adapter + one registry line — see
``_PROVIDERS`` at the bottom. Every adapter returns an :class:`LLMResponse` with
the generated text *and* the dollar cost of the call, so the CEO's spend brakes
work identically regardless of vendor.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from sis import config

DEFAULT_PROVIDER = "anthropic"
DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-8"

# Above about 21000 output tokens the anthropic SDK refuses a non-streaming
# call (it expects one that long to run past ten minutes). The proposer's
# first call stays below this and is sent as before; a longer one streams.
STREAMING_ABOVE = 20000


def needs_streaming(max_tokens: int) -> bool:
    """Whether a call with this output limit must be streamed. Pure."""
    return max_tokens > STREAMING_ABOVE


@dataclass(frozen=True)
class TokenUsage:
    """The token counts of one completion; counts only, never text."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    @classmethod
    def from_anthropic(cls, usage: Any) -> TokenUsage:
        """From an Anthropic ``message.usage`` object (duck-typed, like the cost)."""
        def count(name: str) -> int:
            return int(getattr(usage, name, 0) or 0)

        return cls(input_tokens=count("input_tokens"), output_tokens=count("output_tokens"),
                   cache_creation_tokens=count("cache_creation_input_tokens"),
                   cache_read_tokens=count("cache_read_input_tokens"))


@dataclass(frozen=True)
class LLMResponse:
    """One completion: the text, the model that produced it, and its $ cost.

    ``truncated`` says the answer stopped at the token limit, so it is not the
    whole answer (L33). ``usage`` and ``stop_reason`` are what a provider
    reports about the call, when it reports them.
    """

    text: str
    cost_usd: float
    model: str
    truncated: bool = False
    usage: TokenUsage | None = None
    stop_reason: str | None = None


@dataclass(frozen=True)
class ToolCall:
    """One tool call the model asked for: its id, the tool's name and its input."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolTurn:
    """One turn of a conversation with tools (OMNI-206).

    *response* is the turn as a completion: its text, cost, usage and stop
    reason. *calls* are the tools it asked to run. *content* is the turn as the
    provider returned it, to be sent back unchanged in the next request (the
    thinking that led to a tool call must accompany it).
    """

    response: LLMResponse
    calls: tuple[ToolCall, ...]
    content: tuple[dict[str, Any], ...]


@runtime_checkable
class ToolLLMClient(Protocol):
    """A client that can hold a conversation in which the model calls tools.

    *messages* and *tools* are in the Anthropic Messages format, which other
    providers' adapters translate. *allow_tools* False lets the model answer
    only in text (``tool_choice: none``), which ends the conversation.
    """

    model: str

    def converse(self, *, system: str, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]], max_tokens: int,
                 allow_tools: bool) -> ToolTurn: ...

    def count_input(self, *, system: str, messages: list[dict[str, Any]],
                    tools: list[dict[str, Any]]) -> int:
        """The input tokens a turn with this prompt would bill; free, no completion."""
        ...


class LLMClient(Protocol):
    """Turns a (system, user) prompt into text + the cost of the call."""

    model: str

    def complete(self, *, system: str, user: str, max_tokens: int) -> LLMResponse: ...

    def check(self) -> str:
        """Confirm the key and the model without a completion; the model's name.

        Read-only and free: the preflight's (OMNI-82). Raises what the provider
        raises for a key it rejects or a model it does not serve.
        """
        ...


class AnthropicClient:
    """Claude via the ``anthropic`` SDK (adaptive thinking, cached system prompt)."""

    def __init__(self, model: str = DEFAULT_ANTHROPIC_MODEL) -> None:
        from sis.cost import price

        price(model)   # an unpriced model is refused before any call (L25)
        self.model = model

    def complete(self, *, system: str, user: str, max_tokens: int) -> LLMResponse:
        import anthropic

        from sis.cost import cost_from_usage

        client = anthropic.Anthropic()  # resolves ANTHROPIC_API_KEY from the env
        request: dict[str, Any] = dict(
            model=self.model,
            max_tokens=max_tokens,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            system=[{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
        if needs_streaming(max_tokens):
            # The SDK refuses a non-streaming call this long (OMNI-219): the
            # retry of a cut-off proposal. The final message is the same object.
            with client.messages.stream(**request) as stream:
                message = stream.get_final_message()
        else:
            message = client.messages.create(**request)
        text = "".join(block.text for block in message.content if block.type == "text")
        return LLMResponse(
            text=text, cost_usd=cost_from_usage(message.usage, self.model), model=self.model,
            truncated=message.stop_reason == "max_tokens",
            usage=TokenUsage.from_anthropic(message.usage),
            stop_reason=None if message.stop_reason is None else str(message.stop_reason))

    def converse(self, *, system: str, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]], max_tokens: int,
                 allow_tools: bool) -> ToolTurn:
        """One turn of a tool conversation (OMNI-206); its cost from the turn's usage."""
        import anthropic

        from sis.cost import cost_from_usage

        client = anthropic.Anthropic()
        request: dict[str, Any] = dict(
            model=self.model,
            max_tokens=max_tokens,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            system=[{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}],
            messages=_cache_last_turn(messages),
            tools=tools,
            tool_choice={"type": "auto" if allow_tools else "none"},
        )
        if needs_streaming(max_tokens):
            with client.messages.stream(**request) as stream:
                message = stream.get_final_message()
        else:
            message = client.messages.create(**request)
        text = "".join(block.text for block in message.content if block.type == "text")
        calls = tuple(ToolCall(id=str(block.id), name=str(block.name),
                               input=dict(block.input) if isinstance(block.input, dict) else {})
                      for block in message.content if block.type == "tool_use")
        response = LLMResponse(
            text=text, cost_usd=cost_from_usage(message.usage, self.model), model=self.model,
            truncated=message.stop_reason == "max_tokens",
            usage=TokenUsage.from_anthropic(message.usage),
            stop_reason=None if message.stop_reason is None else str(message.stop_reason))
        content = tuple(block.model_dump(exclude_none=True) for block in message.content)
        return ToolTurn(response=response, calls=calls, content=content)

    def count_input(self, *, system: str, messages: list[dict[str, Any]],
                    tools: list[dict[str, Any]]) -> int:
        """``POST /v1/messages/count_tokens``: free, and no completion is made."""
        import anthropic

        request: dict[str, Any] = dict(model=self.model, system=system,
                                       messages=messages, tools=tools)
        counted = anthropic.Anthropic().messages.count_tokens(**request)
        return int(counted.input_tokens)

    def check(self) -> str:
        """``GET /v1/models/{model}``: a 401 for a bad key, a 404 for an unknown model."""
        import anthropic

        info = anthropic.Anthropic().models.retrieve(self.model)
        return str(getattr(info, "display_name", None) or self.model)


def _cache_last_turn(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """*messages* with a cache breakpoint on the first and the last user turn. Pure.

    A tool conversation sends everything again each turn; cached, the
    repeated prefix bills at a tenth of the input rate. The caller's list is
    not changed.
    """
    marked: list[dict[str, Any]] = []
    users = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    points = {users[0], users[-1]} if users else set()
    for i, message in enumerate(messages):
        content = message.get("content")
        if i in points and isinstance(content, list) and content:
            blocks = [dict(block) for block in content]
            blocks[-1]["cache_control"] = {"type": "ephemeral"}
            message = {**message, "content": blocks}
        elif i in points and isinstance(content, str):
            message = {**message, "content": [{"type": "text", "text": content,
                                               "cache_control": {"type": "ephemeral"}}]}
        marked.append(message)
    return marked


def _make_anthropic(model: str | None) -> LLMClient:
    return AnthropicClient(model or DEFAULT_ANTHROPIC_MODEL)


# provider name -> factory(model_override) -> client. Register a new vendor here;
# add its per-model prices to sis/cost.py so the spend brakes stay accurate.
_PROVIDERS: dict[str, Callable[[str | None], LLMClient]] = {
    "anthropic": _make_anthropic,
}


def get_llm_client(provider: str | None = None, model: str | None = None) -> LLMClient:
    """The configured LLM client (``proposer.llm_provider`` / ``proposer.llm_model``)."""
    name = (provider or str(config.get("proposer.llm_provider"))).lower()
    factory = _PROVIDERS.get(name)
    if factory is None:
        known = ", ".join(sorted(_PROVIDERS))
        raise ValueError(
            f"Unknown proposer.llm_provider={name!r} (SIS_LLM_PROVIDER; known: {known})"
        )
    return factory(model or config.get("proposer.llm_model"))


def ensure_proposer_priced() -> None:
    """Refuse a real proposer whose model has no price, before any spend (L25).

    The client is built only when a proposal is asked for, inside a cycle whose
    spend is already reserved; checking here lets ``run_cycle`` refuse first.
    """
    if config.get("proposer.backend") == "stub":
        return
    from sis.cost import UnpricedModel

    try:
        get_llm_client()
    except UnpricedModel as exc:
        raise UnpricedModel(
            f"{exc}. Set proposer.llm_model (SIS_LLM_MODEL) to a priced model, or add "
            "its published rates to sis/cost.py.") from None


def configured_model(provider: str | None = None, model: str | None = None) -> str:
    """The model ``get_llm_client`` would use — for the episodic log; makes no call."""
    return get_llm_client(provider, model).model
