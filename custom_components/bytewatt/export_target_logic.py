"""Export-target controller — pure scheduling maths, no Home Assistant imports.

Keeps the decision logic free of HA so it can be unit-tested directly against
real numbers. The HA-facing layer (export_target.py) owns entities, timers and
the SettingsManager write; everything here is a pure function of the inputs.

The problem
-----------
The inverter's feed-in setting is a power CAP in watts, not an energy target.
To export a chosen number of kWh across a window, spread the remaining energy
over the remaining time and re-solve periodically as reality drifts:

    required_w = remaining_kwh / remaining_hours * 1000

Re-solving each tick is what makes this self-correcting: if the battery hit its
cutoff SOC, or house load ate the export, or solar over-delivered, the next
tick simply recomputes against what is actually left.

Design decisions (confirmed with the system owner)
--------------------------------------------------
* Progress is measured from a BASELINE captured at window start, so only
  in-window export counts. Solar exported earlier in the day is ignored.
* Behind pace -> re-spread evenly over the remaining time (smooth ramp), never
  a burst to catch up.
* Battery protection is delegated entirely to the existing Grid Feed-in
  Cutoff SOC. This module never reasons about SOC.
* Target met -> command 0 W for the rest of the window rather than over-export.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Optional

# Command changes smaller than this are suppressed. The inverter step is
# 100 W, but the goal is to react to a genuinely good/bad stretch rather than
# track every wobble, so the deadband is deliberately much wider than the
# hardware resolution.
MIN_POWER_STEP_W = 100

# Deadband for a routine adjustment: below this the existing cap is close
# enough and not worth an API write.
POWER_DEADBAND_W = 300

# Minimum wall-clock gap between writes to the inverter. The settings
# endpoint is rate-limited (the reason the staged Submit button exists), and
# the control loop only needs to correct for sustained over/under-delivery,
# not minute-to-minute noise. A ~3 hour window therefore sees at most ~6
# writes.
MIN_WRITE_INTERVAL = timedelta(minutes=30)

# Below this, treat the window as effectively over. Dividing a residual target
# by a near-zero remaining time explodes toward the clamp for no benefit.
MIN_REMAINING_HOURS = 1.0 / 60.0  # one minute


@dataclass(frozen=True)
class ExportPlan:
    """The controller's decision for one tick."""

    active: bool                 # inside the window with a live target?
    power_w: int                 # feed-in cap to command (0 when idle/complete)
    remaining_kwh: float         # still to export before hitting target
    remaining_hours: float       # left in the window
    exported_kwh: float          # achieved so far this window
    target_kwh: float
    reason: str                  # human-readable, surfaced as a sensor attribute
    complete: bool = False       # target reached inside the window

    @property
    def progress_pct(self) -> float:
        if self.target_kwh <= 0:
            return 0.0
        return min(100.0, round(self.exported_kwh / self.target_kwh * 100, 1))


def in_window(now: time, start: time, end: time) -> bool:
    """Is `now` inside [start, end)? Handles windows crossing midnight.

    End is exclusive so a window ending 21:01 stops controlling AT 21:01
    rather than commanding one final pointless write.
    """
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end  # wraps midnight


def hours_remaining(now: datetime, end: time) -> float:
    """Hours from `now` until the next occurrence of wall-clock `end`."""
    end_dt = now.replace(hour=end.hour, minute=end.minute,
                         second=end.second, microsecond=0)
    if end_dt <= now:
        end_dt += timedelta(days=1)
    return (end_dt - now).total_seconds() / 3600.0


def window_length_hours(start: time, end: time) -> float:
    """Total window length in hours, midnight-crossing aware."""
    s = start.hour + start.minute / 60 + start.second / 3600
    e = end.hour + end.minute / 60 + end.second / 3600
    length = e - s
    if length <= 0:
        length += 24.0
    return length


def compute_plan(
    *,
    now: datetime,
    start: time,
    end: time,
    target_kwh: float,
    feed_in_today_kwh: Optional[float],
    baseline_kwh: Optional[float],
    enabled: bool,
    max_power_w: int,
) -> ExportPlan:
    """Decide the feed-in cap for this tick.

    `baseline_kwh` is `feed_in_today_kwh` sampled at window start; None means
    the window has not opened yet this cycle. Passing the raw daily counter
    plus a baseline (rather than a pre-differenced value) keeps the caller
    trivial and the arithmetic auditable here.
    """
    def idle(reason: str) -> ExportPlan:
        return ExportPlan(
            active=False, power_w=0, remaining_kwh=0.0,
            remaining_hours=0.0, exported_kwh=0.0,
            target_kwh=target_kwh, reason=reason,
        )

    if not enabled:
        return idle("Disabled")
    if target_kwh <= 0:
        return idle("No target set")
    if not in_window(now.time(), start, end):
        return idle("Outside export window")
    if feed_in_today_kwh is None:
        return idle("Export sensor unavailable")
    if baseline_kwh is None:
        return idle("Waiting for window baseline")

    # The daily counter resets at local midnight. For a window crossing
    # midnight the post-reset reading is smaller than the baseline; treat the
    # baseline as 0 from that point so progress keeps accumulating instead of
    # going negative.
    if feed_in_today_kwh < baseline_kwh:
        exported = max(0.0, feed_in_today_kwh)
    else:
        exported = feed_in_today_kwh - baseline_kwh

    remaining_kwh = target_kwh - exported
    remaining_h = hours_remaining(now, end)

    if remaining_kwh <= 0:
        return ExportPlan(
            active=True, power_w=0, remaining_kwh=0.0,
            remaining_hours=remaining_h, exported_kwh=exported,
            target_kwh=target_kwh, complete=True,
            reason=f"Target met ({exported:.2f} of {target_kwh:.2f} kWh)",
        )

    if remaining_h <= MIN_REMAINING_HOURS:
        return ExportPlan(
            active=True, power_w=0, remaining_kwh=remaining_kwh,
            remaining_hours=remaining_h, exported_kwh=exported,
            target_kwh=target_kwh,
            reason=(f"Window ending — {remaining_kwh:.2f} kWh short "
                    f"({exported:.2f} of {target_kwh:.2f} kWh)"),
        )

    raw_w = remaining_kwh / remaining_h * 1000.0
    power_w = int(round(raw_w / MIN_POWER_STEP_W) * MIN_POWER_STEP_W)
    power_w = max(0, min(power_w, max_power_w))

    if raw_w > max_power_w:
        reason = (f"Capped at {max_power_w} W — need {raw_w:.0f} W for "
                  f"{remaining_kwh:.2f} kWh in {remaining_h:.2f} h")
    else:
        reason = (f"On track — {remaining_kwh:.2f} kWh over {remaining_h:.2f} h "
                  f"= {power_w} W")

    return ExportPlan(
        active=True, power_w=power_w, remaining_kwh=remaining_kwh,
        remaining_hours=remaining_h, exported_kwh=exported,
        target_kwh=target_kwh, reason=reason,
    )


def should_write(
    current_w: Optional[float],
    desired_w: int,
    *,
    now: Optional[datetime] = None,
    last_write: Optional[datetime] = None,
    min_interval: timedelta = MIN_WRITE_INTERVAL,
    deadband_w: int = POWER_DEADBAND_W,
) -> bool:
    """Decide whether to push a new feed-in cap to the inverter.

    Two independent brakes, because the settings endpoint is rate-limited and
    the loop only needs to correct sustained drift, not track noise:

      1. Deadband — ignore changes smaller than ``deadband_w``.
      2. Rate limit — at most one write per ``min_interval`` (default 30 min).

    Three cases deliberately BYPASS the rate limit, because delaying them by
    up to half an hour would be visibly wrong:

      * the current value is unknown (nothing staged yet — establish control)
      * stopping (``desired_w == 0``), e.g. target met or window ending;
        continuing to export for another 30 minutes would overshoot badly
      * resuming from a stop (``current_w == 0`` with a live target), so a
        window that opens just after a write isn't dead for 30 minutes

    ``now``/``last_write`` are optional so callers that manage their own
    timing (and the deadband-only tests) can omit them.
    """
    if current_w is None:
        return True

    # Stop immediately; never sit at a non-zero cap when the answer is zero.
    if desired_w == 0:
        return current_w != 0

    # Start immediately when coming off a stop.
    if current_w == 0:
        return True

    if abs(current_w - desired_w) < deadband_w:
        return False

    if now is not None and last_write is not None:
        if now - last_write < min_interval:
            return False

    return True
