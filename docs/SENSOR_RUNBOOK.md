# Sensor capture and replay

The sensor implementation reads observations, not a fuel-market model. The
current EIA adapter captures five weekly series into a local trace; scenario
generation (OMNI-34), approved domain fixtures (OMNI-35) and prediction-error
reporting (OMNI-36) remain separate work. No control-loop consumer uses the
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

## Times and historical limits

`event_time` places EIA's period date at midnight in `America/New_York`. That is
an adapter convention, not an exact measurement instant supplied by EIA. Stock
series describe a week ending Friday; retail-price series use Monday dates.

The first capture of a series backfills its available history. Those values
receive `reconstructed: true`, with `known_at` estimated from the series'
publication schedule and capped at fetch time. The implementation uses Thursday
17:00 Eastern for the prior Friday's stocks and Tuesday 17:00 Eastern for
Monday retail prices. These are assumptions covering regular releases and
ordinary holiday shifts, not verified release timestamps.

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

Captures serialize with an exclusive file lock. Appends are synced; caught
write failures roll back the attempted block. The writer refuses destinations
inside `specs/`, symlinks at the opened path and multiply linked files. These
checks do not justify treating runtime staging data as immutable evidence.

An incomplete final line, invalid UTF-8/JSON, inconsistent capture block or a
record rejected on read causes a refusal. No reader silently skips that damage.
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
