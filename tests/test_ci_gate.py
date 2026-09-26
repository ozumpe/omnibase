"""scripts/ci_gate.py — the two decisions behind CI's fast path (OMNI-60).

Both are pure, and both are where a mistake is silent: a docs classifier that is
too generous skips the suites on a code change, and a gate that treats
"skipped" as "passed" goes green on a PR where nothing ran.
"""

from __future__ import annotations

import importlib.util
import types
from typing import Any

import pytest

from sis.paths import PROJECT_ROOT


def _load() -> types.ModuleType:
    # scripts/ is not a package; load it the way CI runs it, by path.
    spec = importlib.util.spec_from_file_location(
        "ci_gate", PROJECT_ROOT / "scripts" / "ci_gate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ci_gate = _load()


# --- what counts as docs-only ----------------------------------------------------


@pytest.mark.parametrize(
    "paths",
    [
        ["README.md"],
        ["docs/KNOWN_ISSUES.md", "CLAUDE.md"],
        ["ray_self_improving_control_loop.svg"],
        ["docs/img/flow.PNG"],
    ],
)
def test_documentation_alone_is_docs_only(paths: list[str]) -> None:
    assert ci_gate.docs_only(paths)


@pytest.mark.parametrize(
    "paths",
    [
        ["sis/gauntlet.py"],
        ["docs/KNOWN_ISSUES.md", "sis/gauntlet.py"],   # one code file is enough
        ["config.yml"],
        ["pyproject.toml"],
        ["poetry.lock"],
        [".github/workflows/ci.yml"],                  # CI itself is code
        ["requirements.txt"],
        ["docs/diagram.mmd"],                          # unknown kind: full run
        ["specs/sort/tests.py"],
        ["Dockerfile.gauntlet"],
        [],                                            # nothing to classify
    ],
    ids=lambda paths: ",".join(paths) or "empty",
)
def test_anything_else_runs_everything(paths: list[str]) -> None:
    assert not ci_gate.docs_only(paths)


# --- the required `test` check -------------------------------------------------


def _needs(docs_only: str, **results: str) -> dict[str, Any]:
    needs: dict[str, Any] = {
        "changes": {"result": "success", "outputs": {"docs_only": docs_only}}}
    for job in ("lint", "fast", "serve", "docs"):
        needs[job] = {"result": results.get(job, "skipped"), "outputs": {}}
    return needs


def test_a_code_change_passes_when_all_three_suites_pass() -> None:
    ok, reason = ci_gate.verdict(
        _needs("false", lint="success", fast="success", serve="success"))
    assert ok, reason


@pytest.mark.parametrize("failed", ["lint", "fast", "serve"])
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
def test_a_code_change_fails_if_any_suite_did_not_succeed(failed: str, result: str) -> None:
    # "skipped" is in the list on purpose: a broken `if:` that skipped the
    # suites on a code change must not read as a pass.
    results = {"lint": "success", "fast": "success", "serve": "success", failed: result}
    ok, reason = ci_gate.verdict(_needs("false", **results))
    assert not ok
    assert failed in reason


def test_a_docs_change_passes_on_the_docs_job_alone() -> None:
    ok, reason = ci_gate.verdict(_needs("true", docs="success"))
    assert ok, reason
    assert "docs-only" in reason


@pytest.mark.parametrize("result", ["failure", "skipped"])
def test_a_docs_change_still_needs_the_docs_job(result: str) -> None:
    ok, _ = ci_gate.verdict(_needs("true", docs=result))
    assert not ok


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
def test_nothing_passes_if_the_classifier_did_not_run(result: str) -> None:
    needs = _needs("false", lint="success", fast="success", serve="success")
    needs["changes"]["result"] = result
    ok, reason = ci_gate.verdict(needs)
    assert not ok
    assert "classifier" in reason


def test_a_missing_classifier_output_means_the_full_run_was_required() -> None:
    # No output at all must not be read as "docs-only".
    needs = _needs("", docs="success")
    ok, _ = ci_gate.verdict(needs)
    assert not ok
