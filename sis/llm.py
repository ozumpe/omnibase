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
from typing import Protocol

from sis import config

DEFAULT_PROVIDER = "anthropic"
DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-8"


@dataclass(frozen=True)
class LLMResponse:
    """One completion: the text, the model that produced it, and its $ cost.

    ``truncated`` says the answer stopped at the token limit, so it is not the
    whole answer (L33).
    """

    text: str
    cost_usd: float
    model: str
    truncated: bool = False


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
        message = client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            system=[{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(block.text for block in message.content if block.type == "text")
        return LLMResponse(
            text=text, cost_usd=cost_from_usage(message.usage, self.model), model=self.model,
            truncated=message.stop_reason == "max_tokens")

    def check(self) -> str:
        """``GET /v1/models/{model}``: a 401 for a bad key, a 404 for an unknown model."""
        import anthropic

        info = anthropic.Anthropic().models.retrieve(self.model)
        return str(getattr(info, "display_name", None) or self.model)


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
