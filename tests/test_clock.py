"""Tests for the Clock port (OMNI-23).

The point of the port is that event time becomes *stateable* rather than
whatever the host happened to think when a gate ran. So the tests care about
three things: that a replayed instant is stable, that an ambiguous or
out-of-order one is refused loudly, and that nothing in the gate path smuggles
wall-clock time into what a candidate sees.
"""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime, timedelta

import pytest

from sis.backtest import FIXTURE_SCHEMA, Backtest, build_script, parse_fixture, plan_entry
from sis.clock import Clock, ReplayClock, WallClock, now_iso, parse_event_time
from sis.episodic import event_from_cycle_result

EPISODE = datetime(2026, 3, 31, 23, 59, 59, tzinfo=UTC)


# --- the port -------------------------------------------------------------


def test_both_adapters_satisfy_the_port() -> None:
    assert isinstance(WallClock(), Clock)
    assert isinstance(ReplayClock(EPISODE), Clock)


def test_the_wall_clock_is_timezone_aware_and_utc() -> None:
    # A naive timestamp means "10:30, somewhere" -- unreplayable by construction.
    moment = WallClock().now()
    assert moment.tzinfo is not None
    assert moment.utcoffset() == timedelta(0)


# --- replay ---------------------------------------------------------------


def test_a_replay_clock_does_not_move_on_its_own() -> None:
    # Constructed and never advanced, it is a frozen clock -- which is how a
    # gate becomes testable without sleeping.
    clock = ReplayClock(EPISODE)
    assert clock.now() == clock.now() == EPISODE


def test_advancing_moves_event_time_forward() -> None:
    clock = ReplayClock(EPISODE)
    later = EPISODE + timedelta(days=1)
    clock.advance_to(later)
    assert clock.now() == later


def test_advancing_to_the_same_instant_is_allowed() -> None:
    # Two episodes recorded with the same timestamp is ordinary, not an error.
    clock = ReplayClock(EPISODE)
    clock.advance_to(EPISODE)
    assert clock.now() == EPISODE


def test_a_replay_clock_refuses_to_rewind() -> None:
    """An out-of-order trace is a data bug and must fail loudly.

    A clock that silently rewound would still be *reproducible* -- and wrong,
    which is strictly worse, because the replay would look trustworthy.
    """
    clock = ReplayClock(EPISODE)
    with pytest.raises(ValueError, match="cannot move backwards"):
        clock.advance_to(EPISODE - timedelta(seconds=1))
    assert clock.now() == EPISODE  # and it did not partially apply


def test_a_replay_clock_can_be_built_from_a_recorded_timestamp() -> None:
    assert ReplayClock.at("2026-03-31T23:59:59+00:00").now() == EPISODE


def test_replaying_the_same_trace_twice_produces_identical_event_times() -> None:
    trace = [
        "2026-01-01T00:00:00Z",
        "2026-02-01T12:30:00Z",
        "2026-03-31T23:59:59Z",
    ]

    def replay() -> list[str]:
        clock = ReplayClock.at(trace[0])
        seen = []
        for moment in trace:
            clock.advance_to(parse_event_time(moment, where="trace"))
            seen.append(clock.now().isoformat())
        return seen

    assert replay() == replay()


# --- parsing --------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "2026-03-31T23:59:59Z",
        "2026-03-31T23:59:59+00:00",
        "2026-03-31T18:59:59-05:00",
    ],
)
def test_recorded_forms_of_the_same_instant_all_parse_to_it(raw: str) -> None:
    # 'Z' is the form a recorder is most likely to emit, so it has to work.
    assert parse_event_time(raw, where="f.json") == EPISODE


def test_a_naive_timestamp_is_refused_rather_than_assumed_to_be_utc() -> None:
    """Defaulting the offset would invent information the recorder never gave.

    The failure mode that prevents: a model that looks subtly wrong during
    replay, hours off, with nothing in the log pointing at the cause.
    """
    with pytest.raises(ValueError, match="no timezone"):
        parse_event_time("2026-03-31T23:59:59", where="specs/x/q1.json")


def test_a_parse_failure_names_the_file_it_came_from() -> None:
    with pytest.raises(ValueError, match="specs/x/q1.json"):
        parse_event_time("not a timestamp", where="specs/x/q1.json")


def test_a_non_utc_offset_is_preserved_as_the_same_instant() -> None:
    parsed = parse_event_time("2026-03-31T18:59:59-05:00", where="f.json")
    assert parsed == EPISODE
    assert parsed.utcoffset() == timedelta(hours=-5)


def test_constructing_a_replay_clock_from_a_naive_instant_is_refused() -> None:
    with pytest.raises(ValueError, match="no timezone"):
        ReplayClock(datetime(2026, 3, 31, 23, 59, 59))


def test_advancing_to_a_naive_instant_is_refused() -> None:
    clock = ReplayClock(EPISODE)
    with pytest.raises(ValueError, match="no timezone"):
        clock.advance_to(datetime(2026, 4, 1, 0, 0, 0))


# --- now_iso and the episodic log ----------------------------------------


def test_now_iso_defaults_to_the_wall_clock() -> None:
    # Adoption is opt-in per call site, so every existing caller is unchanged.
    before = WallClock().now()
    stamped = datetime.fromisoformat(now_iso())
    assert stamped >= before


def test_now_iso_uses_the_clock_it_is_given() -> None:
    assert now_iso(ReplayClock(EPISODE)) == EPISODE.isoformat()


def test_an_episodic_event_can_be_stamped_in_event_time() -> None:
    """A replay driver stamps reconstructed episodes in the timeline they belong to.

    The default stays the wall clock: `ts` normally records when the *engine*
    ran a cycle, which is audit-trail time and should not be movable.
    """
    event = event_from_cycle_result({"status": "promoted"}, clock=ReplayClock(EPISODE))
    assert event.ts == EPISODE.isoformat()


def test_two_replays_of_one_cycle_result_produce_the_same_timestamp() -> None:
    result = {"status": "promoted"}
    first = event_from_cycle_result(result, clock=ReplayClock(EPISODE))
    second = event_from_cycle_result(result, clock=ReplayClock(EPISODE))
    assert first.ts == second.ts


def test_the_episodic_default_is_still_the_wall_clock() -> None:
    event = event_from_cycle_result({"status": "promoted"})
    assert datetime.fromisoformat(event.ts).tzinfo is not None


# --- fixtures carry event time as a real instant --------------------------


def test_a_fixture_event_time_is_parsed_not_carried_as_a_string() -> None:
    # "First-class field, not an incidental key in a JSON blob": a fixture whose
    # timestamp is broken is broken *as recorded*, and finding that out during a
    # replay -- after the window to re-record has closed -- is the whole failure
    # this field exists to avoid.
    raw = json.dumps(
        {"schema": FIXTURE_SCHEMA, "args": [1], "event_time": "2026-03-31T23:59:59Z"}
    )
    assert parse_fixture(raw, where="f.json").event_time == EPISODE


def test_a_fixture_with_a_naive_event_time_is_rejected_at_parse() -> None:
    raw = json.dumps(
        {"schema": FIXTURE_SCHEMA, "args": [1], "event_time": "2026-03-31T23:59:59"}
    )
    with pytest.raises(ValueError, match="no timezone"):
        parse_fixture(raw, where="specs/x/q1.json")


def test_a_fixture_may_still_omit_event_time() -> None:
    # No adapter records traces yet; requiring it would block every fixture on
    # a recorder that does not exist.
    raw = json.dumps({"schema": FIXTURE_SCHEMA, "args": [1]})
    assert parse_fixture(raw, where="f.json").event_time is None


def test_an_offset_event_time_normalises_to_the_same_instant() -> None:
    tokyo = json.dumps(
        {"schema": FIXTURE_SCHEMA, "args": [1], "event_time": "2026-04-01T08:59:59+09:00"}
    )
    assert parse_fixture(tokyo, where="f.json").event_time == EPISODE


# --- the acceptance criterion --------------------------------------------


def test_the_same_trace_produces_byte_identical_gate_input() -> None:
    """Nothing in the generated gate script comes from the host's clock.

    This is the property that makes a backtest reproducible at all, and it is
    the kind that decays silently -- someone adds a generated-at header and the
    gate keeps passing while replay quietly stops being deterministic. Cheap to
    pin, expensive to notice the absence of.
    """
    backtest = Backtest(name="q1", fixture="specs/x/q1_fixture.json",
                        expect="specs/x/q1_expect.json")
    plan = [
        plan_entry(
            backtest,
            fixture_path=pathlib.Path("/s/0_f.json"),
            expect_path=pathlib.Path("/s/0_e.json"),
        )
    ]

    def build() -> str:
        return build_script(
            candidate_path="/s/target.py", comparators_path="/s/comparators.py",
            oracle_path="/s/oracle.py", entry="f", plan=plan,
        )

    assert build() == build()


def test_the_gate_script_contains_no_timestamp() -> None:
    # The complement of the test above, stated as intent rather than as a
    # comparison: a script that never mentions a date cannot drift.
    script = build_script(
        candidate_path="/s/c.py", comparators_path="/s/cmp.py",
        oracle_path=None, entry="f", plan=[],
    )
    assert str(datetime.now(UTC).year) not in script
