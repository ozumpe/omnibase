"""sis.proposer — code proposer (stub or a real LLM).

Selected by the ``SIS_PROPOSER`` env var:

- ``stub`` (default): returns a hand-written replacement from the active
  contract's ``stub_candidate_path``. Zero API calls — keeps the loop runnable
  offline and in tests.
- any other value (``claude``, ``llm``, …): calls a real LLM via the
  provider-agnostic :mod:`sis.llm` port (default provider ``anthropic``,
  ``SIS_LLM_PROVIDER`` / ``SIS_LLM_MODEL`` to change it) with the current
  source + the contract's required interface, and returns a fully
  type-annotated, faster version. The candidate then goes through the full
  gauntlet exactly like the stub's — the LLM is never trusted, the
  deterministic gates are.

This module owns the prompt and the code extraction; *which* model answers is
:mod:`sis.llm`'s job, so the loop isn't tied to one vendor. Provider SDKs are
optional, imported lazily, so the default path needs neither a package nor a key.

Nothing here is contract-specific by name. What used to be a single hardcoded
target (``sum_of_divisors``) is now read off the ``OptimizationContract`` passed
in: the entry function's signature and trusted-reference source come from
``contract.load_oracle()`` (inspecting the oracle module rather than duplicating
its content as separate prompt fields), and the required public API — which can
be more than just ``entry``, e.g. ``sum_of_divisors``'s contract also tests a
``benchmark()`` helper — comes from including the contract's own acceptance
tests verbatim. The system prompt carries no contract-specific text at all, so
it stays fully cacheable regardless of which contract is active.
"""

from __future__ import annotations

import inspect
import pathlib
import re

from sis import config, llm
from sis.contract import OptimizationContract, default_contract
from sis.paths import PROJECT_ROOT

MODEL = llm.DEFAULT_ANTHROPIC_MODEL  # back-compat: the default model
MAX_TOKENS = 8000

# Cost + model of the most recent propose() call (0.0 / None for the stub). The
# CEO reads the cost to enforce the spend cap and the cost-per-accepted SLO.
_last_cost_usd: float = 0.0
_last_model: str | None = None


def last_cost_usd() -> float:
    """USD cost of the most recent propose() call (0.0 for the stub)."""
    return _last_cost_usd


def last_model() -> str | None:
    """Model of the most recent propose() call (None for the stub)."""
    return _last_model

# Stable, cacheable instructions (the system prompt). Kept byte-frozen — no
# timestamps, per-request, or per-contract data — so prompt caching reuses it
# across cycles regardless of which contract is active.
_SYSTEM_PROMPT = """\
You optimise a single Python module for speed without changing its behaviour.

Hard requirements for your output:
- Return ONLY the complete replacement module source — no prose, no markdown
  fences, no explanation.
- Preserve the required public API exactly, as specified in the user message:
  the entry function's signature, and any other function its acceptance tests
  require.
- Results must be identical to the trusted reference for every input; only the
  implementation may change.
- The module MUST be fully type-annotated and pass `mypy --strict`.
- Use only the Python standard library.

The candidate you return will be validated by a strict gauntlet (ast.parse →
mypy --strict → interface check → the acceptance tests → differential
correctness against the reference on random inputs → benchmark vs baseline).
Anything that changes results, fails typing, is missing a required function, or
isn't faster is rejected, so prioritise correctness, then speed."""


def propose(
    current_source: str,
    baseline_latency: float,
    *,
    contract: OptimizationContract | None = None,
) -> str:
    """Return a candidate replacement for *contract*'s target module.

    *contract* says what the candidate must implement and be judged by; it
    falls back to the bootstrap ``sum_of_divisors`` contract when omitted, so
    existing callers keep working unchanged.
    """
    global _last_cost_usd, _last_model
    _last_cost_usd = 0.0  # reset; the stub is free
    _last_model = None
    spec = contract if contract is not None else default_contract()
    if config.get("proposer.backend") == "stub":
        return _stub_proposal(spec)
    return _llm_proposal(current_source, baseline_latency, spec)


def _stub_proposal(spec: OptimizationContract) -> str:
    """Hand-written replacement read from the contract's stub_candidate_path."""
    if spec.stub_candidate_path is None:
        raise RuntimeError(
            f"SIS_PROPOSER=stub has no canned candidate for contract {spec.name!r} "
            "(OptimizationContract.stub_candidate_path is unset) — set "
            "SIS_PROPOSER=claude for this contract, or add a stub answer."
        )
    return (PROJECT_ROOT / spec.stub_candidate_path).read_text(encoding="utf-8")


def _user_prompt(current_source: str, baseline_latency: float, spec: OptimizationContract) -> str:
    """Build the per-call prompt: the contract's interface, ground truth, and
    the current source to beat. Everything here is contract-derived, not
    contract-specific — no target name is ever hardcoded."""
    oracle = spec.load_oracle()
    reference = oracle.reference
    signature = f"{spec.entry}{inspect.signature(reference)}"
    reference_source = inspect.getsource(reference)
    tests_source = pathlib.Path(spec.tests_file).read_text(encoding="utf-8")

    return (
        f"Contract: {spec.name!r}. Required entry point:\n\n"
        f"    {signature}\n\n"
        "Trusted reference implementation (defines correct behaviour — your "
        "candidate must produce IDENTICAL results for every input, but should "
        "use a different, faster approach):\n\n"
        f"{reference_source}\n"
        "Acceptance tests your module will be run against (provide every "
        "function they need, not just the entry point):\n\n"
        f"{tests_source}\n"
        f"Current module source (baseline mean latency {baseline_latency:.6f}s; "
        f"your candidate must run in at most {spec.max_latency_ratio:.0%} of that "
        f"— i.e. at least {(1 - spec.max_latency_ratio):.0%} faster):\n\n"
        f"{current_source}\n\n"
        "Return an optimised replacement module that is correct, fully typed, "
        "and faster."
    )


def _llm_proposal(current_source: str, baseline_latency: float, spec: OptimizationContract) -> str:
    """Ask the configured LLM (sis.llm) for a typed, optimised variant."""
    user_prompt = _user_prompt(current_source, baseline_latency, spec)
    response = llm.get_llm_client().complete(
        system=_SYSTEM_PROMPT, user=user_prompt, max_tokens=MAX_TOKENS)
    global _last_cost_usd, _last_model
    _last_cost_usd = response.cost_usd
    _last_model = response.model
    return _extract_code(response.text)


def _extract_code(text: str) -> str:
    """Strip optional markdown fences and return the module source."""
    fence = re.search(r"```(?:python)?\n(.*?)```", text, re.DOTALL)
    return (fence.group(1) if fence else text).strip() + "\n"
