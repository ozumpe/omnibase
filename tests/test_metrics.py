"""Tests for the pure percentile/window helpers (no Ray, no Serve, no clock)."""

import pytest

from sis.metrics import METRIC_KEYS, percentile, summarise


def test_percentile_is_nearest_rank() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    # Nearest-rank: every result is a value that actually occurred, which is the
    # honest thing to compare against an SLO.
    assert percentile(values, 50) == 5.0
    assert percentile(values, 95) == 10.0
    assert percentile(values, 100) == 10.0
    assert percentile(values, 0) == 1.0


def test_percentile_is_defined_for_a_single_sample() -> None:
    # A canary window with one request in it is a real state the loop has to
    # report on. statistics.quantiles raises here; this must not.
    assert percentile([0.5], 99) == 0.5


def test_percentile_ignores_input_order() -> None:
    assert percentile([9.0, 1.0, 5.0], 50) == percentile([1.0, 5.0, 9.0], 50)


def test_percentile_rejects_out_of_range_p() -> None:
    with pytest.raises(ValueError, match="0..100"):
        percentile([1.0], 101)


def test_empty_window_is_zeros_not_an_error() -> None:
    # "No traffic yet" is normal early in a canary; the *caller* decides whether
    # the sample count clears its floor (see the window-sizing open problem).
    window = summarise([])
    assert window.samples == 0
    assert window.p95 == 0.0
    assert window.error_rate == 0.0


def test_summarise_reports_error_rate_over_the_window() -> None:
    window = summarise([0.1, 0.2, 0.3, 0.4], errors=1)
    assert window.samples == 4
    assert window.error_rate == 0.25


def test_as_metrics_matches_the_documented_wire_shape() -> None:
    # Callers rely on this shape regardless of which Cloud adapter produced it.
    metrics = summarise([0.1, 0.2], errors=0).as_metrics()
    assert set(metrics) == set(METRIC_KEYS)
    assert all(isinstance(v, float) for v in metrics.values())
