"""OMNI-40: the twin viewer's loading, and the promises it makes about itself.

What these pin: one command's data loads (the recorded autumn-2022 fixture, a
trace through the sanitising door, a generated scenario labelled simulated);
the viewer opens nothing for writing and never reaches Ray or the actors; and it
refuses to start unauthenticated on a non-loopback bind, before loading anything.
The Panel layer is smoke-tested only where the ``ui`` group is installed.
"""

from __future__ import annotations

import ast
import builtins
import datetime
import io
import json
import os
import pathlib
from collections.abc import Iterator, Sequence
from typing import Any

import pytest

from sis import config, prediction_error, twin_view, viewer
from sis.adapters_real import EIA_RULES, eia_period, eia_published
from sis.paths import PROJECT_ROOT
from sis.prediction_error import CALIFORNIA, Question
from sis.sensor_trace import Fetched, capture_lines

UNIT = "Dollars per Gallon"
MODULES = (PROJECT_ROOT / "sis" / "viewer.py", PROJECT_ROOT / "sis" / "twin_view.py")


def _persistence(asked: Sequence[Question]) -> list[Any]:
    def last(state: dict[str, Any]) -> float:
        ca = [r for r in state["readings"] if r["series"] == CALIFORNIA]
        return float(max(ca, key=lambda r: r["event_time"])["value"])

    return prediction_error.ask(last, asked)


@pytest.fixture
def no_writes(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every way this process could open a file for writing, refused."""
    real_open, real_os_open = builtins.open, os.open

    def guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if any(flag in mode for flag in "wax+"):
            raise AssertionError(f"the viewer opened {file!r} for writing ({mode})")
        return real_open(file, mode, *args, **kwargs)

    def guarded_os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC):
            raise AssertionError(f"the viewer opened {path!r} for writing")
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    yield


# -- loading -------------------------------------------------------------------------

def test_the_recorded_fixture_loads_week_by_week(no_writes: None) -> None:
    timeline = viewer.load_fixture(ask=_persistence)
    assert timeline.kind == twin_view.RECORDED and not timeline.simulated
    assert len(timeline.forecasts) == 17     # one per episode
    assert len(timeline.moments()) == 17
    october = [f for f in timeline.forecasts
               if f.week and f.week.date().isoformat() == "2022-10-03"]
    before = [r.value for r in timeline.readings if r.series == CALIFORNIA
              and r.event_time.date().isoformat() == "2022-09-26"]
    expect = json.loads((viewer.FIXTURE / "backtests" / "2022-10-03_expect.json")
                        .read_text(encoding="utf-8"))
    assert october and october[0].predicted == before[-1]      # persistence
    assert october[0].actual == expect["value"]
    # The last episode's actual came out after the fixture was built.
    assert timeline.forecasts[-1].actual_known_at is None


def _trace(prices: dict[datetime.date, float]) -> list[dict[str, Any]]:
    def series(values: dict[datetime.date, float]) -> list[tuple[object, object]]:
        return [(eia_period(day.strftime("%Y%m%d")), value) for day, value in values.items()]

    return capture_lines(
        [], [Fetched(CALIFORNIA, UNIT, series(prices)),
             Fetched(twin_view.US, UNIT, series({day: 3.9 for day in prices}))],
        EIA_RULES, capture="eia-20261015T020000.000000Z-abcdef01",
        fetched_at=datetime.datetime(2026, 10, 15, 2, 0, tzinfo=datetime.UTC),
        published=eia_published,
        provenance={"url": "https://www.eia.gov/opendata/bulk/PET.zip", "sha256": "ab" * 32,
                    "attribution": "Source: U.S. Energy Information Administration (Oct 2026)"})


def test_a_trace_loads_through_the_door_with_its_reconstructed_marks(
        tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mondays = [datetime.date(2022, 9, 5) + datetime.timedelta(weeks=i) for i in range(6)]
    lines = _trace(dict(zip(mondays, (5.05, 5.18, 5.39, 5.60, 6.29, 6.21), strict=True)))
    path = tmp_path / "eia.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    timeline = viewer.load_trace(path, ask=_persistence)
    assert timeline.kind == twin_view.RECORDED
    assert len(timeline.readings) == 12
    assert timeline.reconstructed == {twin_view.reading_key(r) for r in timeline.readings}
    assert [f.predicted for f in timeline.forecasts][:2] == [5.05, 5.18]


def test_a_missing_trace_is_refused(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ValueError, match="capture one first"):
        viewer.load_trace(tmp_path / "none.jsonl")


def test_a_scenario_is_labelled_simulated(no_writes: None) -> None:
    timeline = viewer.load_scenario("supply_shock", seed=3, ask=_persistence)
    assert timeline.simulated
    assert {r.source for r in timeline.readings} == {"sim"}
    assert timeline.forecasts and timeline.reconstructed == frozenset()


# -- read-only, and never the actors -------------------------------------------------

_WRITE_CALLS = {"write_text", "write_bytes", "mkdir", "unlink", "rmdir", "touch", "rename",
                "dump", "connect", "remove", "rmtree", "save"}
_FORBIDDEN_IMPORTS = ("ray", "sis.org", "sis.roles", "sis.self_model", "sis.episodic",
                      "duckdb", "sis.admin", "sis.operator_audit")


def test_the_viewer_has_no_write_path_and_no_way_to_the_actors() -> None:
    for module in MODULES:
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in _WRITE_CALLS, (module.name, node.lineno)
                owner = node.func.value
                assert not (isinstance(owner, ast.Name) and owner.id == "sensor_trace"
                            and node.func.attr == "append"), (module.name, node.lineno)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "open":
                raise AssertionError(f"{module.name}:{node.lineno} calls open()")
            names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for name in names:
                assert not name.startswith(_FORBIDDEN_IMPORTS), (module.name, name)


def test_the_check_above_catches_a_write() -> None:
    # A check that cannot fail asserts nothing.
    tree = ast.parse("import ray\npath.write_text('x')\n")
    calls = [n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    assert set(calls) & _WRITE_CALLS
    assert any(isinstance(n, ast.Import) and n.names[0].name.startswith(_FORBIDDEN_IMPORTS)
               for n in ast.walk(tree))


# -- serving -------------------------------------------------------------------------

def test_it_refuses_to_start_unauthenticated_off_loopback(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("SIS_FRONTEND_AUTH", "none")
    monkeypatch.setenv("SIS_FRONTEND_BIND", "0.0.0.0")  # noqa: S104 - the case refused
    config.reset_config_cache()

    def never(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("loaded or served before refusing")

    monkeypatch.setattr(viewer, "load_fixture", never)
    monkeypatch.setattr(viewer, "sandboxed", never)
    try:
        assert viewer._main([]) == 2
    finally:
        config.reset_config_cache()
    assert "reachable beyond this machine" in capsys.readouterr().err


def test_the_panel_layer_builds(no_writes: None) -> None:
    pytest.importorskip("panel")
    timeline = viewer.load_fixture(ask=_persistence)
    app = viewer.build_replay(timeline)
    app.get_root()    # renders the first moment: chain, charts, forecast table
    viewer.build_app(timeline, _persistence).main[0].get_root()
