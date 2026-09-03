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

def test_write_suppression():
    assert etl.should_write(None, 5000) is True
    assert etl.should_write(5000, 5000) is False
    assert etl.should_write(5000, 5050) is False, "sub-step jitter must not write"
    assert etl.should_write(5000, 5100) is True
    assert etl.should_write(5000, 0) is True, "stopping must take effect at once"
    assert etl.should_write(0, 0) is False


# --------------------------------------------------------------------------
# End-to-end simulation
# --------------------------------------------------------------------------

def test_simulated_window_converges_on_target():
    """Step through a whole evening at the real ~10 min sensor cadence.

    Models export actually delivered as 85% of the commanded cap (house load
    and inverter losses eat the rest) to prove the loop self-corrects rather
    than depending on the first estimate being right.

    Delivery is clamped at the window end: the inverter's feed-in slot closes
    at 21:01, so a rate commanded at 20:59 only runs for two minutes, not a
    full tick. Not clamping it credits export that never happens.
    """
    from datetime import timedelta

    target = 12.0
    exported = 0.0
    writes = 0
    last_cmd = None
    t = datetime(2026, 8, 28, 17, 59)
    end_dt = datetime(2026, 8, 28, 21, 1)
    step = timedelta(minutes=10)

    while t < end_dt:
        p = plan(t, exported, target=target)
        if etl.should_write(last_cmd, p.power_w):
            writes += 1
            last_cmd = p.power_w
        active_h = (min(t + step, end_dt) - t).total_seconds() / 3600
        exported += (last_cmd or 0) * 0.85 / 1000 * active_h
        t += step

    assert exported == pytest.approx(target, rel=0.10), (
        f"converged to {exported:.2f} kWh against a {target} kWh target")
    assert writes < 20, f"too many API writes ({writes}) for one window"


def test_overshoot_is_bounded_by_one_control_interval():
    """Worst-case overshoot: target is hit just after a tick commanded power.

    The controller can only react at tick boundaries, so between ticks it may
    export past the target. That excess is bounded by (commanded rate x tick
    length) and must stay small at a 5 minute control interval — this test
    pins that it is single-digit percent, not unbounded.
    """
    from datetime import timedelta

    target = 12.0
    exported = 0.0
    last_cmd = 0
    t = datetime(2026, 8, 28, 17, 59)
    end_dt = datetime(2026, 8, 28, 21, 1)
    step = timedelta(minutes=5)

    while t < end_dt:
        p = plan(t, exported, target=target)
        last_cmd = p.power_w
        active_h = (min(t + step, end_dt) - t).total_seconds() / 3600
        # 100% delivery: the fastest realistic march toward the target, so
        # this is the worst case for overshooting between ticks.
        exported += last_cmd / 1000 * active_h
        t += step

    overshoot_pct = (exported - target) / target * 100
    assert exported >= target * 0.98, f"undershot at {exported:.2f} kWh"
    assert overshoot_pct < 5.0, (
        f"overshot by {overshoot_pct:.1f}% ({exported:.2f} vs {target} kWh)")


def test_simulation_recovers_from_a_stall():
    """Battery contributes nothing for the first hour, then comes back.

    Mirrors hitting the cutoff SOC or an outage mid-window: the controller
    must re-spread the deficit over what's left instead of giving up.
    """
    from datetime import timedelta

    target = 9.0
    exported = 0.0
    last_cmd = None
    t = datetime(2026, 8, 28, 17, 59)
    end_dt = datetime(2026, 8, 28, 21, 1)
    peak_cmd = 0

    while t < end_dt:
        p = plan(t, exported, target=target)
        last_cmd = p.power_w
        peak_cmd = max(peak_cmd, last_cmd)
        stalled = t < datetime(2026, 8, 28, 19, 0)
        if not stalled:
            exported += last_cmd * 0.9 / 1000 * (10 / 60)
        t += timedelta(minutes=10)

    assert exported > target * 0.85, (
        f"only reached {exported:.2f} of {target} kWh after the stall")
    assert peak_cmd <= MAXW
