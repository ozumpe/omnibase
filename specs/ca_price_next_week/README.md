# `ca_price_next_week`: recorded fixture (OMNI-35)

Built by `scripts/build_fixture.py` from a sensor trace. **17 episodes**, all held out (`Split.HOLDOUT`).

**The question.** When `PET.EMM_EPMRR_PTE_SCA_DPG.W`'s value for a week became known, given everything known then (every series, latest vintage per period, back 364 days), what is its value for the next week? Each `backtests/<week>_fixture.json` holds that as-of view in `args[0]`; each `<week>_expect.json` holds the answer, the value for `<week>`.

**Replay rule** (`sis/replay.py`): by `known_at`; `event_time` is data; mixed anchors (Friday stocks, Monday prices) are not aligned.

**Release check.** Reconstructed times were checked against `release_calendar.json` (checked by Olaf Zumpe on 2026-10-15): each reading's `known_at` is the later of the trace's and the end of its release day.

**Vintages.** Values are the vintage the trace holds; backfilled history is the source's current vintage, so a value revised before the first capture appears revised.

## Provenance

- capture `eia-20261026T013504.178499Z-cc5777eb`, fetched 2026-10-26T01:35:04.178499+00:00: sha256 `cc5777ebd1d8e04f1916c9525741f147f5541ad09157133910ab5655f0f87ebc`, https://www.eia.gov/opendata/bulk/PET.zip
- trace lines, canonical sha256 `59ebf9f1961b5e66cfea26aea828db1b335be5dfc70b6b0f7d525da19a86e402`
- Source: U.S. Energy Information Administration (Oct 2026)

EIA's content is reproduced as published: nothing here is derived from it and labelled EIA's.

## Episodes

- 2022-08-08
- 2022-08-15
- 2022-08-22
- 2022-08-29
- 2022-09-05
- 2022-09-12
- 2022-09-19
- 2022-09-26
- 2022-10-03
- 2022-10-10
- 2022-10-17
- 2022-10-24
- 2022-10-31
- 2022-11-07
- 2022-11-14
- 2022-11-21
- 2022-11-28
