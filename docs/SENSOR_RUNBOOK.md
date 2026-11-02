# Sensor capture and replay

The sensor implementation reads observations, not a fuel-market model. The
current EIA adapter captures five weekly series and one daily one into a local trace, and
generated scenarios (OMNI-34) replay through the simulated sensor, and a
predictor's error can be computed on either (OMNI-36); approved domain
fixtures (OMNI-35) remain separate work. No control-loop consumer uses the
sensor automatically. This guide describes the current working-tree
implementation of OMNI-33, not its availability in older release tags.

## Capture EIA observations

From the repository root:

```bash
poetry install --with real
poetry run python scripts/capture_sensor.py
```

The script downloads EIA's bulk petroleum file `PET.zip` without an API key.
It extracts declared series, sanitises their records, and appends to
`runtime/sensor_traces/eia.jsonl`. It does not need Ray, GitHub, Jira or
Confluence credentials. Run captures explicitly; neither selecting the sensor
backend nor starting `main.py` schedules them.

| Bulk-file series | Observation | Published unit |
|---|---|---|
| `PET.WGTSTP51.W` | PADD 5 total gasoline stocks | Thousand Barrels |
| `PET.EMM_EPMRR_PTE_SCA_DPG.W` | California regular reformulated retail price | Dollars per Gallon |
| `PET.EMM_EPMRR_PTE_Y05LA_DPG.W` | Los Angeles regular reformulated retail price | Dollars per Gallon |
| `PET.EMM_EPMRR_PTE_Y05SF_DPG.W` | San Francisco regular reformulated retail price | Dollars per Gallon |
| `PET.EMM_EPMRR_PTE_NUS_DPG.W` | US regular reformulated retail price | Dollars per Gallon |
| `PET.EER_EPMRR_PF4_Y05LA_DPG.D` | Los Angeles RBOB spot price, daily (licensed, see below) | Dollars per Gallon |

The Los Angeles spot price is Thomson Reuters' data, which EIA republishes
under licence. EIA's reuse policy puts it under "Protected materials", not the
public-domain line: it may be modelled, and its raw values may not be
redistributed. The trace is gitignored. A fixture committed under `specs/`
shows only `EIA_PUBLIC_SERIES` (`scripts/build_fixture.py`), so never this
series; one that shows it is private (see "A private fixture" below). Do not
paste its values into a PR, a ticket or a public log. Spot
prices are released once a week on Wednesday, so a day's value is taken as
known on the first Thursday at least three days later, at 17:00 ET.

Stocks and prices have different reporting geographies and anchor days. This
adapter does not capture production, inter-PADD transfers, traffic or station
sales. Adding a series requires its declared unit, plausible range and
publication assumptions to be reviewed in `sis/adapters_real.py`; generated
code cannot extend that guardrail list.

Every capture prints received, appended, reconstructed and refused counts,
missing series and any periods the publisher no longer sends. Unchanged values
add no new value lines, but a capture header is still appended.

Exit code 0 means none of those problem conditions was reported. Exit code 1
means a refusal, missing series or vanished period; **accepted values may still
have been appended**. Inspect the report and its capture header before retrying.
Fetch/format errors raise rather than producing a normal successful report.

## Check connectivity

`poetry run python scripts/check_connections.py --deep` includes a sensor check
alongside the other adapter checks. It sends a HEAD request to EIA and reports
the local trace summary. It does not download the bulk data, validate its five
series or run the trace through the sanitising boundary. An empty trace can
therefore pass connectivity. Other checks may need configured credentials.

## Read and replay

The configured `sensor.backend` values are `sim` and `real`; see `config.yml`
for the env/CLI layers. `make_sensor("real")` constructs `EIASensor` and performs
no download. Pass the resulting sensor explicitly to a consumer, rather than
looking it up inside an actor. An uncaptured sensor returns no readings.

For example, read observations that became known within a UTC window:

```python
from datetime import datetime, timezone

from sis.adapters import make_sensor
from sis.ports import TimeAxis

sensor = make_sensor("real")
readings = sensor.read(
    datetime(2026, 10, 1, tzinfo=timezone.utc),
    datetime(2026, 10, 8, tzinfo=timezone.utc),
    by=TimeAxis.KNOWN,
)
```

Windows are half-open `[start, end)` and both bounds must have timezones.
`TimeAxis.EVENT` selects the period described; `TimeAxis.KNOWN` selects when a
reading became available. Results are sorted deterministically by the selected
axis, then the other time, source and series. `replay()` in `sis/adapters.py`
advances a supplied `ReplayClock` along that explicit axis.

A read returns the matching revisions, not a deduplicated latest-value snapshot.
An event-time read can therefore include values learned later. Consumers must
enforce their as-of cutoff and resolve revisions explicitly. A known-time replay
can process revisions as they arrive.

`EIASensor.read()` returns readings only, so it cannot show a value EIA has
withdrawn. `EIASensor.absent()` takes the same window and axis, and returns the
periods EIA stated no value for, each with its own `known_at`. A value EIA
published and later blanked appears in `read()` at its first `known_at`, and in
`absent()` from the moment it was blanked. An as-of view that should drop
withdrawn values must take both.

**What a model may see at a moment** is `sis.replay.as_of(readings, absent, t)`,
the rule OMNI-35 decided before the first fixture: a replay runs on `known_at`;
at `t` a model sees, per period, the latest vintage known by `t`, and no value a
later withdrawal hid. Mixed anchors are not aligned. Two vintages of one period
known at the same instant are refused (`AmbiguousVintage`).

## From a trace to a fixture

A fixture is the exam, so it reaches `specs/` only through a human:

1. **Check the release calendar.** `scripts/data/eia_release_calendar_2021_2022.json`
   records when each period was really released (WPSR archive for stocks, ALFRED's
   release dates for retail prices). Compare it with the sources it names, then fill
   in `checked_by` and `checked_on`. An unchecked calendar refuses every build.
2. **Build and stage:** `poetry run python scripts/build_fixture.py`. It reads the
   trace back through the door, gives every reconstructed reading it shows a
   `known_at` no earlier than the end of its release day, refuses a reading the
   calendar does not cover, and stages the draft under
   `runtime/contract_staging/ca_price_next_week/` with its digest.
3. **Review:** `poetry run python -m sis.contract_author review ca_price_next_week`
   lists every file with its hash and the manifest digest.
4. **Promote:** `poetry run python -m sis.contract_author promote ca_price_next_week
   --digest <digest> --reason "..."`. It refuses a different digest or a file changed
   since review, and audits to `runtime/operator_audit.jsonl`. Commit the new
   `specs/` directory through a pull request.

### A private fixture (OMNI-163)

`ca_price_wholesale` shows the licensed LA spot price, so its episodes may not
be committed. The steps are the same, with these differences:

1. **Draft and check the calendar.** It also needs the spot price's releases.
   `python scripts/draft_release_calendar.py urls --sources DIR` prints what to
   save into `DIR` (`curl -sSL -o DIR/<name> '<url>'`; run it again once
   `spot_vintages.html` is saved, for the spot CSVs). `draft --sources DIR`
   writes `scripts/data/eia_release_calendar_2018_2026.json` with
   `checked_by` empty. Check its dates against the saved files and fill in
   both fields. The spot dates come from ALFRED's `DGASUSGULF`, the Gulf Coast
   price from the same weekly EIA release, because ALFRED does not carry the
   LA series. Only dates are kept.
2. **Build:** `python scripts/build_fixture.py --fixture ca_price_wholesale`
   builds every week from 6 January 2020 to the last the trace answers (about
   350 episodes, about 40 MB). A fresh capture backfills its history as
   reconstructed, which needs the calendar, so the build stops at the last
   week the calendar covers and prints it, with the week the trace goes to.
   To build further, extend the calendar and have it checked again. `--last
   YYYY-MM-DD` sets the last week yourself; one past the calendar is refused. Each question is asked on Thursday at 17:00 ET,
   a day after the retail price is out, when Monday's spot price is known.
   The episodes are staged under `private/`, with `private.json` listing the
   sha256 of each.
3. **Review and promote** as above. `specs/ca_price_wholesale/private/` is
   gitignored. Commit only `private.json`, `README.md` and the calendar.
4. **Keep the private files in the artifacts bucket**, not in git:
   `aws s3 sync specs/ca_price_wholesale/private/
   s3://<artifacts bucket>/specs/ca_price_wholesale/private/`. On another
   machine, sync them back into the same directory (`docs/AWS_RUN.md` step
   5a has the commands; a scheduled AWS run fetches and checks them itself,
   OMNI-177). The backtest gate checks
   every file against `private.json` and refuses a missing or different one as
   a `harness:` fault, so a verdict is only reached on the data a human
   promoted.

## Generated scenarios

Scenarios drive a twin into situations the record does not hold (OMNI-34). Each
is a pure function of its kind and seed, so a failure it finds reproduces byte
for byte:

```python
from sis import scenarios
from sis.ports import TimeAxis

generated = scenarios.generate("supply_shock", seed=7,
                               data_events=("missing_week", "republished", "withdrawn"))
sensor = generated.sensor()                    # a SimSensor, read like a trace
at = generated.decision_times()[5]             # when a California price became known
```

The kinds are `supply_shock`, `outage`, `national_move` and `demand_shock`; the
data events a missing week (stated `NA`), a value re-published a week later and
a value withdrawn a week later (`W`). Stocks keep their Friday anchor and prices
their Monday one, with the EIA sensor's reconstructed release times. Magnitudes
stay inside what the real series have shown (`OBSERVED` in the generator)
unless a caller passes `stress=True`, and the scenario then says so.

The generator is `specs/scenarios.py`, the pinned copy the gauntlet uses: the
invariant gate's `fuel_scenario_states` strategy draws a state shaped as a
backtest fixture's argument. `runtime/scenarios.py` is the working copy the
loop may improve; nothing reads it to judge anything (D7). Every scenario
reading has source `sim`, and a backtest fixture holding one is refused:
a scenario may reject a candidate, never be the evidence that promotes one (D4).

## Prediction error

How wrong a predictor is, week by week (OMNI-36). Nothing acts on the number
yet: an error budget and skill against climatology are Phase B's triggers (D9),
and this starts no cycle.

```bash
poetry run python scripts/prediction_error.py                    # autumn 2022, from the EIA trace
poetry run python scripts/prediction_error.py --from 2022-06-01 --to 2023-01-01
poetry run python scripts/prediction_error.py --scenario supply_shock --seed 7
poetry run python scripts/prediction_error.py --scenario outage --seed 3 --events missing_week withdrawn
```

The question is the fixture builder's: at the moment California's price for a
week first became known, given everything known then (`replay.as_of`), what is
next week's price? So the predictor never sees a value before its `known_at`.
Its answer is compared with next week's value as the trace holds it now, the
latest vintage; a week whose next value is missing or withdrawn is skipped,
never scored. The error is signed, predicted minus actual in $/gal, and the
summary gives bias, MAE and RMSE. A persistence forecast in a rising market
shows a negative bias.

The predictor is `runtime/ca_price_persistence.py` once the loop has built it
(the Class-2 contract `ca_price_persistence`), and until then the shipped
persistence stub; `--predictor PATH` names another. It runs in the sandbox
worker, never in the script's process. From code,
`sis.prediction_error.publish(report, telemetry, ref=..., record=self_model.record.remote)`
emits one `prediction.error` event per week and a `prediction.summary`, and
records the summary in the SelfModel's provenance.

A trace replay needs a capture first (`scripts/capture_sensor.py`); without
one the script exits 1 and says so.

## Times and historical limits

`event_time` places EIA's period date at midnight in `America/New_York`. That is
an adapter convention, not an exact measurement instant supplied by EIA. Stock
series describe a week ending Friday; retail-price series use Monday dates.

The first capture of a series backfills its available history. Those values
receive `reconstructed: true`, with `known_at` estimated from the series'
publication schedule and capped at fetch time. The implementation uses Thursday
17:00 Eastern for the prior Friday's stocks and Wednesday 17:00 Eastern for
Monday retail prices. Retail prices came out on Monday in 2022 (Tuesday after a
Monday holiday) and come out on Tuesday now (Wednesday after a government
holiday), so Wednesday is after both; Tuesday was a day early in a holiday week
(OMNI-161). These are assumptions covering regular releases and ordinary holiday
shifts, not verified release timestamps.

Irregular delays can still make reconstruction too early. The week ending
2022-06-17 was released on 2022-06-29, for example. Also, a current bulk file
cannot recover the original value of a historical observation revised before
capture. Check the release archive and vintage evidence for every approved
historical fixture; reconstructed times alone do not establish an unbiased replay.

Once a series has been observed, a new or changed value gets the timestamp of
the fetch that first saw it, with `reconstructed: false`. This is a conservative
availability bound, not its exact publication time. Earlier lines are retained.
Disappearing historical periods are counted, not automatically withdrawn.

## Trace format and trust boundary

Each append block has three possible record kinds:

| Kind | Contents |
|---|---|
| `capture` | Capture identifier, source, fetch timestamp, provenance, per-series counts, bounded rejection details and missing-series list |
| `reading` | Source, series, value, unit, event and known times, reconstruction flag and capture identifier |
| `absent` | Source, series, marker, event and known times, reconstruction flag and capture identifier |

Provenance includes the bulk URL, SHA-256, byte count, member name, source update
metadata and EIA attribution. Publisher values and units remain source content;
derived sums, interpolations and model outputs must be separate artifacts.

`sensor_input.sanitise` accepts declared series with the correct unit, plausible
plain numeric values and timezone-aware times, or records explicit missing
markers as `Absent`; markers are never zero. Wrong shapes, duplicate periods
within a fetch and other refusals are counted. Free text uses `UntrustedText`.

On replay the trace checks capture membership, appended counts and timestamp
consistency, then sanitises value records again. These checks detect malformed
or inconsistent records, not a forged capture that is internally consistent.
The trace is staging, **not an authenticated exam**. Nothing here writes to
`specs/`; domain fixtures require human-approved promotion through the
contract-author path.

## Failure and recovery

Captures serialize with an exclusive file lock, and reads take a shared one, so
a read never sees half a capture's block. Appends are synced; caught
write failures roll back the attempted block. A capture is stamped while it
holds that lock, and one whose fetch time is not after the trace's last capture
is refused and appends nothing: two captures at one instant, or a clock that
stepped back, would leave two revisions of a value that a replay could not
order. If it is refused, compare the host clock with the last capture's
`fetched_at`. A clock that is behind: correct it and capture again. A clock
that is right now but was ahead when that capture was taken: captures resume
once real time passes that `fetched_at`; the values it wrote carry a
`known_at` in the future, which a replay would show late, not early. To
capture sooner, move the trace aside (keeping it) and start a new one, which
backfills with reconstructed times and loses the revisions observed so far. The writer refuses destinations
inside `specs/`, symlinks at the opened path and multiply linked files. These
checks do not justify treating runtime staging data as immutable evidence.

An incomplete final line, invalid UTF-8/JSON, inconsistent capture block,
captures out of fetch-time order or a record rejected on read causes a refusal. No reader silently skips that damage.
To investigate:

1. Stop captures and preserve a byte-for-byte copy of the affected trace.
2. Inspect the reported block and source provenance. Determine whether the
   problem is damaged storage, a source change, or changed sanitising rules.
3. Restore a verified whole-trace backup where available. Keep the damaged copy
   for diagnosis; do not silently truncate it or invent missing capture metadata.
4. If a fresh capture is needed, retain the old trace separately. A new backfill
   cannot recover previously observed revisions or their fetch timestamps.
5. Revalidate the repaired/restored trace through `sensor_trace.read_back()`
   before using it in a fixture. There is no automatic repair command.

For implementation details, see `sis/sensor_trace.py`, `EIASensor` in
`sis/adapters_real.py` and `scripts/capture_sensor.py`.
