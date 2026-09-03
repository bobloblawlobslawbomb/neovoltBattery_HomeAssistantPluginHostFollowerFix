"""Tests for the export-target controller.

Pure logic, no Home Assistant needed. Numbers come from the real system this
was designed against: window 17:59-21:01 AEST, feed_in_today resetting at
local midnight and updating roughly every 10 minutes.

Run: .venv/bin/python -m pytest tests/test_export_target_logic.py -v
"""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, time

import pytest

_LOGIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                      "custom_components", "bytewatt", "export_target_logic.py")
_spec = importlib.util.spec_from_file_location("export_target_logic",
                                               os.path.abspath(_LOGIC))
etl = importlib.util.module_from_spec(_spec)
# Register BEFORE exec: @dataclass looks the module up in sys.modules while
# resolving type hints, and blows up with AttributeError if it isn't there.
sys.modules["export_target_logic"] = etl
_spec.loader.exec_module(etl)

START = time(17, 59)
END = time(21, 1)
MAXW = 20000


def plan(now, exported_since_baseline, target=15.0, enabled=True,
         start=START, end=END, baseline=100.0):
    """Helper: express progress directly rather than juggling two counters."""
    return etl.compute_plan(
        now=now, start=start, end=end, target_kwh=target,
        feed_in_today_kwh=baseline + exported_since_baseline,
        baseline_kwh=baseline, enabled=enabled, max_power_w=MAXW,
    )


# --------------------------------------------------------------------------
# Window arithmetic
# --------------------------------------------------------------------------

def test_in_window_basic():
    assert etl.in_window(time(19, 0), START, END)
    assert etl.in_window(time(17, 59), START, END)
    assert not etl.in_window(time(21, 1), START, END), "end is exclusive"
    assert not etl.in_window(time(17, 58), START, END)
    assert not etl.in_window(time(3, 0), START, END)


def test_in_window_crossing_midnight():
    s, e = time(22, 0), time(6, 0)
    assert etl.in_window(time(23, 30), s, e)
    assert etl.in_window(time(2, 0), s, e)
    assert not etl.in_window(time(12, 0), s, e)


def test_window_length():
    assert etl.window_length_hours(START, END) == pytest.approx(3.0333, abs=1e-3)
    assert etl.window_length_hours(time(22, 0), time(6, 0)) == pytest.approx(8.0)


def test_hours_remaining_crosses_midnight():
    now = datetime(2026, 8, 28, 23, 0)
    assert etl.hours_remaining(now, time(6, 0)) == pytest.approx(7.0)


# --------------------------------------------------------------------------
# Core control behaviour
# --------------------------------------------------------------------------

def test_initial_rate_is_target_over_window():
    """At window start, spread the target evenly: 15 kWh / 3.033 h ~= 4945 W."""
    p = plan(datetime(2026, 8, 28, 17, 59), 0.0, target=15.0)
    assert p.active
    assert p.power_w == pytest.approx(4900, abs=100)
    assert p.remaining_kwh == pytest.approx(15.0)


def test_behind_pace_ramps_up_smoothly():
    """Behind at halfway -> higher rate, but a re-spread, not a burst."""
    # Halfway (19:30), only 3 of 15 kWh done; 1.517 h left for 12 kWh.
    p = plan(datetime(2026, 8, 28, 19, 30), 3.0, target=15.0)
    assert p.power_w == pytest.approx(7900, abs=100)
    assert p.power_w < MAXW
    assert "On track" in p.reason


def test_ahead_of_pace_eases_off():
    """Ahead of schedule -> lower rate, never negative."""
    p = plan(datetime(2026, 8, 28, 19, 30), 12.0, target=15.0)
    assert 0 < p.power_w < 2200
    assert p.remaining_kwh == pytest.approx(3.0)


def test_target_met_commands_zero():
    p = plan(datetime(2026, 8, 28, 20, 0), 15.2, target=15.0)
    assert p.complete
    assert p.power_w == 0
    assert p.remaining_kwh == 0.0
    assert "Target met" in p.reason


def test_unreachable_target_clamps_to_max():
    """Never demand more than the inverter can do; say so in the reason."""
    p = plan(datetime(2026, 8, 28, 20, 55), 0.0, target=15.0)
    assert p.power_w == MAXW
    assert "Capped" in p.reason


def test_end_of_window_stops_commanding():
    """In the last seconds, don't divide by ~0 and spike to the clamp."""
    p = plan(datetime(2026, 8, 28, 21, 0, 30), 10.0, target=15.0)
    assert p.power_w == 0
    assert "short" in p.reason


# --------------------------------------------------------------------------
# Idle / guard conditions
# --------------------------------------------------------------------------

@pytest.mark.parametrize("now,why", [
    (datetime(2026, 8, 28, 12, 0), "before window"),
    (datetime(2026, 8, 28, 22, 0), "after window"),
])
def test_outside_window_is_idle(now, why):
    p = plan(now, 0.0)
    assert not p.active and p.power_w == 0, why


def test_disabled_is_idle():
    p = plan(datetime(2026, 8, 28, 19, 0), 0.0, enabled=False)
    assert not p.active and p.power_w == 0
    assert p.reason == "Disabled"


def test_zero_target_is_idle():
    p = plan(datetime(2026, 8, 28, 19, 0), 0.0, target=0.0)
    assert not p.active and p.power_w == 0


def test_missing_sensor_is_idle_not_a_crash():
    p = etl.compute_plan(
        now=datetime(2026, 8, 28, 19, 0), start=START, end=END,
        target_kwh=15.0, feed_in_today_kwh=None, baseline_kwh=100.0,
        enabled=True, max_power_w=MAXW)
    assert not p.active and p.power_w == 0


def test_missing_baseline_is_idle():
    p = etl.compute_plan(
        now=datetime(2026, 8, 28, 19, 0), start=START, end=END,
        target_kwh=15.0, feed_in_today_kwh=120.0, baseline_kwh=None,
        enabled=True, max_power_w=MAXW)
    assert not p.active


def test_daily_counter_reset_does_not_go_negative():
    """Window crossing midnight: counter resets below the baseline.

    Without special handling `exported` goes negative, the controller thinks
    it is far behind and slams to max power at 00:00.
    """
    p = etl.compute_plan(
        now=datetime(2026, 8, 29, 0, 30), start=time(22, 0), end=time(6, 0),
        target_kwh=10.0, feed_in_today_kwh=0.4,   # reset, then 0.4 exported
        baseline_kwh=25.0, enabled=True, max_power_w=MAXW)
    assert p.exported_kwh == pytest.approx(0.4)
    assert p.remaining_kwh == pytest.approx(9.6)
    assert p.power_w < MAXW


def test_pre_window_solar_is_excluded():
    """Only in-window export counts (confirmed design decision).

    On 28 Aug 14.22 kWh was exported before 17:59. With a 15 kWh target the
    window must still do a full 15 kWh, not treat itself as nearly done.
    """
    p = etl.compute_plan(
        now=datetime(2026, 8, 28, 17, 59), start=START, end=END,
        target_kwh=15.0,
        feed_in_today_kwh=14.22,   # all pre-window solar
        baseline_kwh=14.22,        # captured at window start
        enabled=True, max_power_w=MAXW)
    assert p.exported_kwh == pytest.approx(0.0)
    assert p.remaining_kwh == pytest.approx(15.0)


# --------------------------------------------------------------------------
# Write suppression
# --------------------------------------------------------------------------

def test_write_deadband():
    """Small changes are not worth an API call."""
    assert etl.should_write(None, 5000) is True
    assert etl.should_write(5000, 5000) is False
    assert etl.should_write(5000, 5100) is False, "100 W is inside the deadband"
    assert etl.should_write(5000, 5250) is False
    assert etl.should_write(5000, 5400) is True, "400 W is a real change"


def test_write_rate_limit_30_minutes():
    """At most one routine write per 30 minutes."""
    from datetime import timedelta
    t0 = datetime(2026, 8, 28, 18, 0)

    # A big change, but only 10 minutes since the last write -> hold off.
    assert etl.should_write(5000, 9000, now=t0 + timedelta(minutes=10),
                            last_write=t0) is False
    assert etl.should_write(5000, 9000, now=t0 + timedelta(minutes=29),
                            last_write=t0) is False
    # Past the interval -> allowed.
    assert etl.should_write(5000, 9000, now=t0 + timedelta(minutes=30),
                            last_write=t0) is True


def test_stop_bypasses_the_rate_limit():
    """Target met must take effect at once, not up to 30 minutes later."""
    from datetime import timedelta
    t0 = datetime(2026, 8, 28, 18, 0)
    assert etl.should_write(5000, 0, now=t0 + timedelta(minutes=1),
                            last_write=t0) is True
    assert etl.should_write(0, 0, now=t0 + timedelta(minutes=1),
                            last_write=t0) is False, "already stopped"


def test_resume_from_zero_bypasses_the_rate_limit():
    """A window opening just after a write must not idle for 30 minutes."""
    from datetime import timedelta
    t0 = datetime(2026, 8, 28, 17, 58)
    assert etl.should_write(0, 4900, now=t0 + timedelta(minutes=1),
                            last_write=t0) is True


# --------------------------------------------------------------------------
# End-to-end simulation
# --------------------------------------------------------------------------

def _simulate(target, delivery_fraction, *, stall_until=None,
              tick_minutes=5, start=datetime(2026, 8, 28, 17, 59),
              end_dt=datetime(2026, 8, 28, 21, 1)):
    """Run a window with the real 30-minute write limit enforced.

    Returns (exported_kwh, write_count, commanded_history). `delivery_fraction`
    models how much of the commanded cap is actually delivered to the grid.
    """
    from datetime import timedelta

    exported = 0.0
    writes = 0
    last_write_at = None
    applied_w = None          # what the inverter is actually set to
    history = []
    t = start
    step = timedelta(minutes=tick_minutes)

    while t < end_dt:
        p = etl.compute_plan(
            now=t, start=start.time(), end=end_dt.time(), target_kwh=target,
            feed_in_today_kwh=100.0 + exported, baseline_kwh=100.0,
            enabled=True, max_power_w=MAXW)
        if etl.should_write(applied_w, p.power_w, now=t, last_write=last_write_at):
            applied_w = p.power_w
            last_write_at = t
            writes += 1
        history.append(applied_w or 0)

        active_h = (min(t + step, end_dt) - t).total_seconds() / 3600
        stalled = stall_until is not None and t < stall_until
        if not stalled:
            exported += (applied_w or 0) * delivery_fraction / 1000 * active_h
        t += step

    return exported, writes, history


def test_simulated_window_converges_with_30min_writes():
    """A whole evening, delivering only 85% of the commanded cap.

    Proves the loop still lands near target when it may only adjust every
    30 minutes.
    """
    exported, writes, _ = _simulate(12.0, 0.85)
    assert exported == pytest.approx(12.0, rel=0.12), (
        f"converged to {exported:.2f} kWh against a 12.0 kWh target")
    assert writes <= 8, f"expected ~6 writes in a 3 h window, got {writes}"


def test_write_count_is_bounded_for_a_three_hour_window():
    """~3 h window at one write per 30 min -> a handful, not dozens."""
    _, writes, _ = _simulate(12.0, 0.85, tick_minutes=1)
    assert writes <= 8, f"{writes} writes is too many for a 3 h window"


def test_overshoot_is_bounded_at_full_delivery():
    """Worst case for overshoot: every commanded watt reaches the grid."""
    exported, _, _ = _simulate(12.0, 1.0)
    overshoot_pct = (exported - 12.0) / 12.0 * 100
    assert exported >= 12.0 * 0.95, f"undershot at {exported:.2f} kWh"
    assert overshoot_pct < 12.0, (
        f"overshot by {overshoot_pct:.1f}% ({exported:.2f} vs 12.0 kWh)")


def test_recovers_from_a_bad_period():
    """Nothing exported for the first hour, then normal service resumes.

    This is the case the 30-minute limit has to survive: the controller gets
    at most two adjustments in that first hour, so it must still make up the
    deficit rather than run out of road.
    """
    exported, writes, history = _simulate(
        9.0, 0.9, stall_until=datetime(2026, 8, 28, 19, 0))
    assert exported > 9.0 * 0.80, (
        f"only reached {exported:.2f} of 9.0 kWh after the bad hour")
    assert max(history) <= MAXW
    # It must have ramped UP in response to the deficit.
    assert max(history) > history[0], "never increased the rate to catch up"


def test_holds_a_steady_rate_when_delivery_matches_plan():
    """Delivery exactly as planned -> a flat rate, landing on target.

    No stop command is expected here: the target is met right at the window
    close, so the correct behaviour is to ease down slightly and finish, not
    to cut out early.
    """
    exported, writes, history = _simulate(6.0, 1.0)
    assert exported == pytest.approx(6.0, rel=0.05), (
        f"landed on {exported:.2f} kWh against a 6.0 kWh target")
    assert max(history) <= 2100, "should not need a high rate for an easy target"


def test_very_good_period_ramps_down_and_stops():
    """Solar over-delivers mid-window -> back off, then stop at the target.

    Models an extra 2 kW of export the controller did not command (a sunny
    late afternoon). Since the target counts TOTAL site export, that free
    export must reduce what the battery is asked to contribute.
    """
    from datetime import timedelta

    target = 6.0
    solar_w = 2000.0
    exported = 0.0
    applied = None
    last_write = None
    history = []
    t = datetime(2026, 8, 28, 17, 59)
    end_dt = datetime(2026, 8, 28, 21, 1)
    step = timedelta(minutes=5)

    while t < end_dt:
        p = etl.compute_plan(
            now=t, start=t.replace(hour=17, minute=59).time(), end=end_dt.time(),
            target_kwh=target, feed_in_today_kwh=100.0 + exported,
            baseline_kwh=100.0, enabled=True, max_power_w=MAXW)
        if etl.should_write(applied, p.power_w, now=t, last_write=last_write):
            applied = p.power_w
            last_write = t
        history.append(applied or 0)
        active_h = (min(t + step, end_dt) - t).total_seconds() / 3600
        exported += ((applied or 0) + solar_w) / 1000 * active_h
        t += step

    assert history[0] > history[-1], "should have ramped down as solar delivered"
    assert history[-1] == 0, "should have stopped once the target was met"
