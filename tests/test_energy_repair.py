"""Tests for redistributing the daily energy counter along the cloud day curve."""
import pytest

from tests.module_loader import load_integration_module

energy_repair = load_integration_module("energy_repair")
plan_repair = energy_repair.plan_repair
curve_points = energy_repair.curve_points

HOUR_ENDS = [(h + 1) * 60 for h in range(12)]  # hours 00:00 .. 11:00, anchor = 11:00
# 600 W from 08:00 to 12:00, nothing before.
SUNNY_MORNING = [(0, 0.0), (480, 0.0), (481, 600.0), (720, 600.0)]


def changes(base_state, cumulative):
    return [b - a for a, b in zip([base_state, *cumulative], cumulative)]


def test_lump_after_late_upload_is_spread_over_the_morning() -> None:
    # Cloud unreachable until 11:xx: 2400 Wh all booked into the 11:00 hour.
    states = [0.0] * 11 + [2400.0]

    target = plan_repair(1000.0, states, 3400.0, HOUR_ENDS, SUNNY_MORNING)

    assert target is not None and len(target) == 11
    hourly = changes(0, target)
    assert hourly[:8] == [0] * 8
    assert hourly[8:] == pytest.approx([600, 600, 600], abs=6)
    # The anchor hour keeps the remainder, so the day total is unchanged.
    assert 2400 - target[-1] == pytest.approx(600, abs=6)


def test_day_matching_the_curve_is_left_alone() -> None:
    states = [0.0] * 8 + [590.0, 1210.0, 1800.0, 2400.0]

    assert plan_repair(0.0, states, 2400.0, HOUR_ENDS, SUNNY_MORNING) is None


def test_missing_hour_rows_are_recreated() -> None:
    states = [0.0] * 8 + [600.0, None, None, 2400.0]

    target = plan_repair(0.0, states, 2400.0, HOUR_ENDS, SUNNY_MORNING)

    assert target is not None
    assert target[9] == pytest.approx(1200, abs=10)  # 08:00–10:00


def test_counter_that_missed_midnight_is_refused() -> None:
    states = [0.0] * 11 + [2400.0]

    with pytest.raises(ValueError, match="does not match counter"):
        plan_repair(0.0, states, 3000.0, HOUR_ENDS, SUNNY_MORNING)


def test_energy_without_curve_is_refused() -> None:
    states = [0.0] * 11 + [2400.0]

    with pytest.raises(ValueError, match="curve is empty"):
        plan_repair(0.0, states, 2400.0, HOUR_ENDS, [(0, 0.0), (720, 0.0)])


def test_dark_day_needs_nothing() -> None:
    assert plan_repair(5.0, [0.0] * 12, 5.0, HOUR_ENDS, [(0, 0.0), (720, 0.0)]) is None


def test_curve_points_uses_sole_grid_power_series() -> None:
    chart = {"x_axis": ["07:45", "07:50"], "series": [{"type": "grid_power", "data": [3.8, -1.0]}]}

    assert curve_points(chart) == [(465, 3.8), (470, 0.0)]


def test_curve_points_refuses_ambiguous_grid_series() -> None:
    chart = {
        "x_axis": ["12:00"],
        "series": [{"type": "grid_power", "data": [1.0]}, {"type": "load_power", "data": [2.0]}],
    }

    with pytest.raises(ValueError, match="no usable power series"):
        curve_points(chart)
