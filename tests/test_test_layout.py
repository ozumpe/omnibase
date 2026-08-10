"""The test suite's own configuration is load-bearing, so it gets tests.

The default ``pytest`` run excludes the ``serve`` marker to keep the inner loop
fast. That makes a bare ``pytest`` narrower than "the suite passes" — fine for a
developer mid-change, dangerous as a merge gate. This repo has been bitten by
that exact shape once: OMNI-16, where the CI trigger filtered on the PR's *base*
branch, so a PR stacked on a feature branch skipped CI entirely and could merge
looking green having verified nothing.

So the arrangement is pinned rather than trusted: the marker is registered, the
rule that applies it covers every module that starts Serve, and CI runs the
half the default run skips.
"""

from __future__ import annotations

import tomllib

from sis.paths import PROJECT_ROOT
from tests.conftest import SERVE_FIXTURES, SERVE_MODULES

CI_WORKFLOW = PROJECT_ROOT / ".github/workflows/ci.yml"
PYPROJECT = PROJECT_ROOT / "pyproject.toml"
TESTS_DIR = PROJECT_ROOT / "tests"

# Built at runtime rather than written literally, so this file does not match
# its own detector below. (It did, first time.)
_SERVE_CALLS = tuple(f"serve.{verb}(" for verb in ("start", "run"))


def _pytest_config() -> dict[str, object]:
    with PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    tool = data["tool"]
    assert isinstance(tool, dict)
    config = tool["pytest"]["ini_options"]
    assert isinstance(config, dict)
    return config


def test_the_serve_marker_is_registered() -> None:
    # An unregistered marker is a warning, not an error, so a typo in
    # `pytest.mark.serve` would silently mark nothing and slow nothing down.
    markers = _pytest_config()["markers"]
    assert isinstance(markers, list)
    assert any(str(m).startswith("serve:") for m in markers)


def test_the_default_run_excludes_the_serve_marker_and_parallelises() -> None:
    addopts = str(_pytest_config()["addopts"])
    assert "not serve" in addopts
    assert "-n auto" in addopts


def test_every_module_that_starts_serve_is_covered_by_the_marking_rule() -> None:
    """A new Serve module must join the rule, not quietly rejoin the fast run.

    Static rather than collection-based on purpose: this has to fail when the
    module is *added*, not only when someone happens to run the slow half.
    """
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        if path.name == "test_test_layout.py":
            continue
        source = path.read_text(encoding="utf-8")
        if not any(call in source for call in _SERVE_CALLS):
            continue
        module = path.stem
        defines_serve_fixture = any(f"def {name}(" in source for name in SERVE_FIXTURES)
        assert module in SERVE_MODULES or defines_serve_fixture, (
            f"{path.name} starts Ray Serve but nothing marks it: add it to "
            "SERVE_MODULES in tests/conftest.py, or give it one of the "
            f"{sorted(SERVE_FIXTURES)} fixtures"
        )


def test_the_split_module_keeps_its_fast_half_in_the_default_run() -> None:
    """test_serving.py is deliberately part-fast, part-slow.

    Marking it wholesale would be the easy mistake and would silently drop its
    pure helper tests — the ones worth running on every save — out of the
    default run.
    """
    assert "test_serving" not in SERVE_MODULES
    source = (TESTS_DIR / "test_serving.py").read_text(encoding="utf-8")
    assert "def slots(" in source, "the fixture the marking rule keys on has moved"


def test_ci_runs_the_half_the_default_run_skips() -> None:
    """The expensive half of not repeating OMNI-16.

    If this fails, the Serve tests run *nowhere*: not locally (excluded by
    default) and not in CI. That is worse than having no marker at all, because
    the tick still goes green.
    """
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "-m serve" in workflow, (
        "CI must run the serve-marked tests explicitly; the default addopts "
        "exclude them, so without this step they run nowhere"
    )


def test_the_serve_gate_in_ci_is_serial_via_a_flag_xdist_understands() -> None:
    """They bind a fixed port and share one Ray cluster, so workers fight.

    Specifically ``-n 0`` and not ``-p no:xdist``: disabling the plugin leaves
    ``addopts``' own ``-n auto`` unparsed, and pytest exits on an unrecognised
    argument instead of running anything. That failure looks like a broken CI
    config rather than a broken test, so it is worth pinning the working form.
    """
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    serve_line = next(line for line in workflow.splitlines() if "-m serve" in line)
    assert "-n 0" in serve_line
    assert "no:xdist" not in serve_line


def test_the_files_this_module_asserts_about_exist() -> None:
    # Without this, every assertion above could pass vacuously if a path moved.
    assert CI_WORKFLOW.is_file()
    assert PYPROJECT.is_file()


def test_the_marking_rule_actually_selects_the_serve_modules() -> None:
    """The rule in conftest is what does the work, so check it, not just config.

    Deliberately *not* a serve-marked canary that fails if it ever executes:
    such a test can only pass by never running, which means it fails in the very
    CI gate that runs the slow half. Asserting the rule's inputs is the same
    guarantee without the contradiction.
    """
    assert SERVE_MODULES, "no module is marked slow; the fast run is not fast"
    for module in SERVE_MODULES:
        assert (TESTS_DIR / f"{module}.py").is_file(), (
            f"SERVE_MODULES names {module}, which no longer exists — the rule is "
            "silently covering nothing"
        )
    for fixture in SERVE_FIXTURES:
        assert any(
            f"def {fixture}(" in path.read_text(encoding="utf-8")
            for path in TESTS_DIR.glob("test_*.py")
        ), f"SERVE_FIXTURES names {fixture}, which no test module defines"
