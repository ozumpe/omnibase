# Sensor runbook

How to capture EIA data, build fixtures from it, and measure prediction error.
Run every command from the repository root. Bucket used below:
`sis-first-run-artifacts-696644743351` (the artifacts bucket).

## Weekly: nothing to do

The Friday AWS run captures EIA's Wednesday release into the bucket's
`traces/eia.jsonl` (OMNI-214). That file is the canonical trace. A failed
capture leaves it unchanged, pages you over SNS, and the run carries on. A
missed week is not lost: the next capture records it.

## Once: seed the bucket

Do this once, from the machine that held the trace until now (your Mac):

```bash
aws s3api put-object --bucket sis-first-run-artifacts-696644743351 --key traces/eia.jsonl --body runtime/sensor_traces/eia.jsonl --if-none-match '*'
aws s3 ls s3://sis-first-run-artifacts-696644743351/traces/
```

`--if-none-match '*'` refuses if a trace is already there. From then on, never
capture into a local copy by hand, or the two drift apart.

## Get a local copy of the trace

```bash
aws s3 cp s3://sis-first-run-artifacts-696644743351/traces/eia.jsonl runtime/sensor_traces/eia.jsonl
```

## Capture by hand (only if you must)

Go through the bucket, so nothing is overwritten:

```bash
aws s3api get-object --bucket sis-first-run-artifacts-696644743351 --key traces/eia.jsonl runtime/sensor_traces/eia.jsonl
# note the "ETag" it prints, e.g. "\"abc123...\""
poetry install --with real
poetry run python scripts/capture_sensor.py
aws s3api put-object --bucket sis-first-run-artifacts-696644743351 --key traces/eia.jsonl --body runtime/sensor_traces/eia.jsonl --if-match '"abc123..."'
```

If `put-object` refuses, someone else wrote first: start again from `get-object`.

### What a capture does

- Downloads EIA's bulk file `PET.zip` (about 56 MB, no key), sanitises the
  declared series and appends to `runtime/sensor_traces/eia.jsonl`.
- Derives the refinery-outage flags into `runtime/sensor_traces/events.jsonl`
  (`--no-events` skips it).
- Scores the live predictor on newly scorable weeks into the episodic error
  series, `runtime/episodic_scored.jsonl` (`--no-score` skips it).
- Prints received, appended, reconstructed and refused counts.
- **Exit 0:** no problem reported. **Exit 1:** a refusal, a missing series or a
  vanished period; values may still have been appended, so read the report
  before retrying. Fetch or format errors raise.

### Series captured

| Bulk-file series | Observation | Unit |
|---|---|---|
| `PET.WGTSTP51.W` | PADD 5 gasoline stocks | Thousand Barrels |
| `PET.EMM_EPMRR_PTE_SCA_DPG.W` | California retail price | $/gal |
| `PET.EMM_EPMRR_PTE_Y05LA_DPG.W` | Los Angeles retail price | $/gal |
| `PET.EMM_EPMRR_PTE_Y05SF_DPG.W` | San Francisco retail price | $/gal |
| `PET.EMM_EPMRR_PTE_NUS_DPG.W` | US retail price | $/gal |
| `PET.EER_EPMRR_PF4_Y05LA_DPG.D` | Los Angeles RBOB spot, daily (**licensed**) | $/gal |
| `PET.W_NA_YUP_R50_PER.W` | PADD 5 refinery utilisation (events only) | % |

**The LA spot price is Thomson Reuters' data.** Model it, never publish its
values: not in git, a PR, a ticket or a public log. The trace is gitignored,
and a committed fixture shows only `EIA_PUBLIC_SERIES`. A fixture that needs
the spot price is private (below).

Adding a series means reviewing its unit, range and release schedule in
`sis/adapters_real.py`. Generated code cannot extend that list.

## Check connectivity

```bash
poetry run python scripts/check_connections.py --deep
```

This only sends a HEAD request to EIA and summarises the local trace. It does
not download or validate data, so an empty trace passes.

## Build a fixture

A fixture reaches `specs/` only through `promote` with a digest you reviewed.
Every build reads the trace through the sanitising door and refuses a reading
the release calendar does not cover.

### Public: `ca_price_next_week`

```bash
poetry run python scripts/build_fixture.py
poetry run python -m sis.contract_author review ca_price_next_week
poetry run python -m sis.contract_author promote ca_price_next_week --digest <digest from review> --reason "..."
```

Commit the new `specs/` directory through a pull request.

### Private: `ca_price_wholesale`

```bash
poetry run python scripts/build_fixture.py --fixture ca_price_wholesale
poetry run python -m sis.contract_author review ca_price_wholesale
poetry run python -m sis.contract_author promote ca_price_wholesale --digest <digest from review> --reason "..."
aws s3 sync specs/ca_price_wholesale/private s3://sis-first-run-artifacts-696644743351/specs/ca_price_wholesale/private/
```

- Commit only `private.json`, `README.md` and the calendar. `private/` is gitignored.
- Once `private.json` is promoted, a plain build rebuilds exactly its weeks.
  New weeks come only by rotation.
- The build stops at the last week the release calendar covers and prints it.
  `--last YYYY-MM-DD` picks an earlier end.
- On another machine, sync the private files back (AWS_RUN.md step 5a). The
  scheduled AWS run fetches and checks them itself. The backtest gate refuses
  a missing or changed file as a `harness:` fault.

### Rotate the held-out weeks (OMNI-225)

When the held-out judgements run out (`python -m sis.admin holdout`), or earlier:

1. Extend the release calendar past the new weeks (next section).
2. Build, review and promote the one draft:
   ```bash
   poetry run python scripts/build_fixture.py --fixture ca_price_wholesale --rotate
   ```
   It needs at least `brakes.holdout_rotation_min_weeks` fresh weeks, and with
   fewer it names the week that makes enough.
3. Sync `private/` to the bucket (as above) and open a PR with `private.json`,
   `training.json`, `README.md` and the calendar.

The new weeks become the held-out set and the old ones join the training split.

### Training split (OMNI-205)

```bash
poetry run python scripts/build_fixture.py --fixture ca_price_wholesale --training
```

Then review and promote as above and commit `training.json` through a PR.

- Public series only. The LA spot price is dropped.
- Starts at the calendar's first week (3 December 2018); `--first YYYY-MM-DD` changes it.
- `--events` adds the refinery-outage flags as context.
- The proposer sees it; no gate reads it. A split with a record on or after the
  first held-out week is refused.

### Extend the release calendar (OMNI-190)

The calendar records when each period was really released. CI re-drafts it
from committed source copies and signs it; an unsigned calendar refuses every
build.

```bash
python scripts/draft_release_calendar.py urls --sources DIR --last YYYY-MM-DD
# download each printed URL into DIR (curl -sSL -o DIR/<name> '<url>'), run again for the CSVs
python scripts/draft_release_calendar.py save --sources DIR --last YYYY-MM-DD
python scripts/draft_release_calendar.py draft --sources scripts/data/release_calendar_sources --last YYYY-MM-DD
python scripts/draft_release_calendar.py sign
```

Commit the result through a PR. Keep the raw downloads out of git (bucket).
`save` replaces every CSV value with `x`, because the spot prices are licensed.

## Prediction error

How wrong a predictor is, week by week, and its skill against climatology
(1 − MAE(model) / MAE(climatology); above 0 beats climatology).

```bash
poetry run python scripts/prediction_error.py                    # autumn 2022
poetry run python scripts/prediction_error.py --from 2022-06-01 --to 2023-01-01
poetry run python scripts/prediction_error.py --scenario supply_shock --seed 7
poetry run python scripts/score_capture.py                       # append to the error series
poetry run python scripts/score_capture.py --trace PATH --from 2022-08-01
```

- The question: when California's price for a week first became known, what
  is next week's price? The predictor never sees a value before its `known_at`.
- Error is predicted minus actual, in $/gal. The summary gives bias, MAE, RMSE.
- Climatology is the latest price plus last year's change for that ISO week.
- The predictor is `runtime/ca_price_persistence.py` (or the shipped stub);
  `--predictor PATH` names another. It runs in the sandbox, never in-process.
- The error series scores each week once per predictor version. A revised
  actual adds a new row; it never overwrites. `main.py --loop --loop-trigger
  model-error` acts on the series (OMNI-197).

## Scenarios

Seeded, reproducible stress inputs (OMNI-34). They can reject a candidate,
never promote one (D4).

```python
from sis import scenarios
generated = scenarios.generate("supply_shock", seed=7,
                               data_events=("missing_week", "republished", "withdrawn"))
sensor = generated.sensor()
```

Kinds: `supply_shock`, `outage`, `national_move`, `demand_shock`.
`stress=True` allows magnitudes beyond what the real series have shown.
`specs/scenarios.py` is the pinned copy the gauntlet uses; `runtime/scenarios.py`
is the loop's working copy and judges nothing.

## Reading the trace from code

```python
from datetime import datetime, timezone
from sis.adapters import make_sensor
from sis.ports import TimeAxis

sensor = make_sensor("real")          # no download
readings = sensor.read(datetime(2026, 10, 1, tzinfo=timezone.utc),
                       datetime(2026, 10, 8, tzinfo=timezone.utc), by=TimeAxis.KNOWN)
```

- Windows are `[start, end)` with timezones. `EVENT` = the period described,
  `KNOWN` = when it became available.
- A read returns every revision. `absent()` returns periods EIA stated no value
  for, including withdrawn ones.
- What a model may see at time `t` is `sis.replay.as_of(readings, absent, t)`:
  per period, the latest vintage known by `t`, minus withdrawals.

## Times

- `event_time` is EIA's period date at midnight New York time. Stocks are weeks
  ending Friday; retail prices are Monday dates.
- A first capture backfills history marked `reconstructed: true`, with
  `known_at` estimated: Thursday 17:00 ET for stocks, Wednesday 17:00 ET for
  retail, spot prices the first Thursday at least three days later at 17:00 ET.
- Irregular delays can make an estimate too early (week ending 2022-06-17 came
  out 2022-06-29). The release calendar is what fixtures trust, not these estimates.
- After the first capture, a new or changed value is stamped with the fetch
  that saw it (`reconstructed: false`).

## When something goes wrong

- **Capture refused because its time is not after the last capture:** check the
  machine's clock against the last capture's `fetched_at`. Fix the clock and retry.
- **Trace refused on read** (cut-off line, bad JSON, inconsistent block): there
  is no automatic repair.
  1. Stop captures and keep a byte-for-byte copy.
  2. Restore the previous version from the bucket (S3 versioning keeps every write):
     ```bash
     aws s3api list-object-versions --bucket sis-first-run-artifacts-696644743351 --prefix traces/eia.jsonl
     aws s3api get-object --bucket sis-first-run-artifacts-696644743351 --key traces/eia.jsonl --version-id <VersionId> eia-restored.jsonl
     ```
  3. Check it reads back through `sis.sensor_trace.read_back()` before using it.
  4. A fresh backfill is the last resort: it loses every revision seen so far.

Code: `sis/sensor_trace.py`, `EIASensor` in `sis/adapters_real.py`,
`scripts/capture_sensor.py`, `sis/events.py`, `sis/error_series.py`.
