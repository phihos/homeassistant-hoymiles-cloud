"""Repair hourly statistics of the daily energy sensor from the cloud day curve.

When the inverter or Home Assistant loses the cloud for a while, the daily
counter catches up in one step and the recorder books the whole gap into the
hour the data returned in (e.g. a morning's yield shown at 11:00). The cloud
keeps the full power curve, so the energy already counted for the day can be
redistributed over the hours it was actually produced. The day total and the
latest hourly row are never changed, only the hours before it.
"""
from __future__ import annotations

from datetime import date, timedelta
import logging
from typing import Any, Sequence

_LOGGER = logging.getLogger(__name__)

# An hour is only rewritten when it is off by more than this (Wh) and by more
# than REL_TOLERANCE of its target, so normal curve-vs-counter noise is left alone.
ABS_TOLERANCE_WH = 100.0
REL_TOLERANCE = 0.25


def curve_points(chart: dict[str, Any]) -> list[tuple[int, float]]:
    """Return (minute of day, watts) from a decoded day power chart."""
    series = {s["type"]: s["data"] for s in chart.get("series", [])}
    # Stations without meter/battery report the inverter output as grid_power.
    data = series.get("pv_power") or (series.get("grid_power") if len(series) == 1 else None)
    if data is None:
        raise ValueError(f"no usable power series in day chart: {sorted(series)}")
    points = []
    for label, watts in zip(chart["x_axis"], data):
        hours, minutes = label.split(":")
        points.append((int(hours) * 60 + int(minutes), max(0.0, float(watts))))
    return points


def cumulative_wh(points: Sequence[tuple[int, float]], minute: float) -> float:
    """Energy (Wh) from midnight to ``minute``, trapezoid over the curve."""
    total = 0.0
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        if t1 <= t0 or minute <= t0:
            continue
        hi = min(minute, t1)
        p_hi = p0 + (p1 - p0) * (hi - t0) / (t1 - t0)
        total += (p0 + p_hi) / 2 * (hi - t0) / 60
    return total


def plan_repair(
    base_sum: float,
    states: Sequence[float | None],
    anchor_sum: float,
    hour_end_minutes: Sequence[int],
    points: Sequence[tuple[int, float]],
) -> list[float] | None:
    """Return corrected cumulative day energy per hour, or None if nothing to do.

    ``states`` holds the recorded counter value at the end of each hour of the
    day up to and including the anchor (the latest recorded hour, which must be
    present); missing hours are None. ``hour_end_minutes`` gives the local
    minute of day each of those hours ends at. The result covers every hour
    before the anchor.
    """
    anchor_state = states[-1]
    day_energy = anchor_sum - base_sum
    if anchor_state is None or abs(day_energy - anchor_state) > 1:
        # The counter did not restart from zero at midnight (gap over midnight);
        # redistributing would also have to fix the day total. Leave it alone.
        raise ValueError(
            f"day total {day_energy:.0f} Wh does not match counter {anchor_state} Wh"
        )
    curve_total = cumulative_wh(points, hour_end_minutes[-1])
    if day_energy <= 0:
        return None
    if curve_total <= 0:
        raise ValueError(f"{day_energy:.0f} Wh counted but the day curve is empty")
    scale = day_energy / curve_total
    target = [round(scale * cumulative_wh(points, m)) for m in hour_end_minutes[:-1]]

    previous_target = previous_state = 0.0
    needs_repair = False
    for state, cum in zip(states, target):
        if state is None:
            needs_repair = True
            break
        have, want = state - previous_state, cum - previous_target
        if abs(have - want) > max(ABS_TOLERANCE_WH, REL_TOLERANCE * want):
            needs_repair = True
            break
        previous_state, previous_target = state, cum
    else:
        have = anchor_state - previous_state
        want = day_energy - previous_target
        needs_repair = abs(have - want) > max(ABS_TOLERANCE_WH, REL_TOLERANCE * want)
    return target if needs_repair else None


async def async_repair_day(hass, api, station_id: str, statistic_id: str, day: date) -> bool:
    """Redistribute ``day``'s recorded energy along the cloud curve. True if rows were written."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import (
        async_import_statistics,
        get_metadata,
        statistics_during_period,
    )
    from homeassistant.util import dt as dt_util

    # Walk the day in UTC so DST days get their 23 or 25 hours.
    start = dt_util.as_utc(dt_util.start_of_local_day(day))
    end = dt_util.as_utc(dt_util.start_of_local_day(day + timedelta(days=1)))
    recorder = get_instance(hass)
    rows = (
        await recorder.async_add_executor_job(
            statistics_during_period,
            hass,
            start - timedelta(days=7),
            end,
            {statistic_id},
            "hour",
            None,
            {"state", "sum"},
        )
    ).get(statistic_id, [])
    before = [r for r in rows if r["start"] < start.timestamp()]
    by_start = {r["start"]: r for r in rows if r["start"] >= start.timestamp()}
    if not by_start:
        return False
    anchor = max(by_start)
    hours = []
    t = start
    while t.timestamp() <= anchor:
        hours.append(t)
        t += timedelta(hours=1)
    base_sum = before[-1]["sum"] if before else by_start[anchor]["sum"] - by_start[anchor]["state"]

    chart = await api.get_station_day_power(station_id, day.isoformat())
    points = curve_points(chart)
    hour_end_minutes = []
    for h in hours:
        local_end = dt_util.as_local(h + timedelta(hours=1))
        minute = local_end.hour * 60 + local_end.minute
        hour_end_minutes.append(minute if local_end.date() == day else 24 * 60)
    states = [by_start.get(h.timestamp(), {}).get("state") for h in hours]
    target = plan_repair(base_sum, states, by_start[anchor]["sum"], hour_end_minutes, points)
    if target is None:
        return False

    metadata = (
        await recorder.async_add_executor_job(
            lambda: get_metadata(hass, statistic_ids={statistic_id})
        )
    )[statistic_id][1]
    async_import_statistics(
        hass,
        metadata,
        [
            {"start": h, "state": float(cum), "sum": base_sum + cum}
            for h, cum in zip(hours, target)
        ],
    )
    _LOGGER.info(
        "Redistributed %s Wh of %s on %s over %d hours along the cloud day curve",
        round(by_start[anchor]["state"]),
        statistic_id,
        day,
        len(target),
    )
    return True
