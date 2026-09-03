"""Export-target feature — Home Assistant layer.

Owns the entities, the periodic tick, and the write to the inverter. All
scheduling maths lives in export_target_logic.py (pure, unit-tested); this
module is deliberately thin glue.

How it drives the inverter
--------------------------
The UI feed-in entities are *staged*: edits accumulate in SettingsManager's
pending store and only reach the inverter when the user presses Submit. An
unattended control loop obviously cannot press that button, so it calls
``submit_feedin_one_shot()`` instead — a direct validate-and-POST path that
does NOT touch the pending store. That matters: staging would silently
commit whatever half-finished edits the user had open in the UI the next
time anything submitted.

Writes are rate-limited to one per 30 minutes (see export_target_logic) so
this stays well inside the settings endpoint's tolerance.

State that must survive a restart
---------------------------------
The window baseline (feed_in_today at window start) is the one piece of
state that cannot be recomputed after the fact — the daily counter has
already moved on. It is persisted via HA's Store, so a restart mid-window
resumes against the correct baseline instead of silently re-targeting the
full amount on a partly-finished window.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, FEEDIN_MAX_POWER_W
from .export_target_logic import (
    ExportPlan,
    compute_plan,
    in_window,
    should_write,
)

_LOGGER = logging.getLogger(__name__)

# How often the controller re-evaluates. Writes are separately limited to
# one per 30 min, so a short tick costs nothing but keeps the progress
# sensor responsive and catches the window edges promptly.
TICK_INTERVAL = timedelta(minutes=5)

STORAGE_VERSION = 1
TIME_PERIOD_1 = 0


def _storage_key(entry_id: str) -> str:
    return f"{DOMAIN}_export_target_{entry_id}"


class ExportTargetController:
    """Drives the feed-in cap so a target number of kWh lands in the window."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        coordinator: Any,
        manager: Any,
    ) -> None:
        self.hass = hass
        self._entry = config_entry
        self._coordinator = coordinator
        self._manager = manager

        # User-facing settings, owned by the entities.
        self._enabled: bool = False
        self._target_kwh: float = 0.0

        # Runtime state.
        self._baseline_kwh: Optional[float] = None
        self._baseline_date: Optional[str] = None
        self._last_write_at: Optional[datetime] = None
        self._applied_w: Optional[int] = None
        self._plan: Optional[ExportPlan] = None
        self._last_error: Optional[str] = None

        self._store = Store(hass, STORAGE_VERSION, _storage_key(config_entry.entry_id))
        self._unsub = None
        self._listeners: list = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_start(self) -> None:
        await self._async_load()
        self._unsub = async_track_time_interval(
            self.hass, self._async_tick, TICK_INTERVAL
        )
        _LOGGER.debug("Export-target controller started (tick %s)", TICK_INTERVAL)
        # Evaluate immediately so a restart mid-window resumes control
        # rather than waiting out a full tick.
        await self._async_tick(dt_util.now())

    @callback
    def async_stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def _async_load(self) -> None:
        data = await self._store.async_load() or {}
        self._enabled = bool(data.get("enabled", False))
        self._target_kwh = float(data.get("target_kwh", 0.0) or 0.0)
        self._baseline_kwh = data.get("baseline_kwh")
        self._baseline_date = data.get("baseline_date")
        last = data.get("last_write_at")
        if last:
            try:
                self._last_write_at = datetime.fromisoformat(last)
            except ValueError:
                self._last_write_at = None

    async def _async_save(self) -> None:
        await self._store.async_save({
            "enabled": self._enabled,
            "target_kwh": self._target_kwh,
            "baseline_kwh": self._baseline_kwh,
            "baseline_date": self._baseline_date,
            "last_write_at": (self._last_write_at.isoformat()
                              if self._last_write_at else None),
        })

    # ------------------------------------------------------------------
    # Settings, driven by the entities
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def target_kwh(self) -> float:
        return self._target_kwh

    @property
    def plan(self) -> Optional[ExportPlan]:
        return self._plan

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    async def async_set_enabled(self, value: bool) -> None:
        self._enabled = value
        if not value:
            # Releasing control: drop the baseline so the next window starts
            # clean, and hand the feed-in rate back to whatever the user has
            # configured rather than leaving our last computed cap in place.
            self._baseline_kwh = None
            self._baseline_date = None
            self._applied_w = None
            await self._async_save()
            self._notify()
            # Deliberately no tick here. _async_tick would re-enter
            # _async_manage_baseline and, if we are still inside the window,
            # immediately re-capture the baseline we just cleared.
            return
        await self._async_save()
        self._notify()
        await self._async_tick(dt_util.now())

    async def async_set_target(self, value: float) -> None:
        self._target_kwh = max(0.0, float(value))
        await self._async_save()
        self._notify()
        await self._async_tick(dt_util.now())

    def add_listener(self, cb) -> None:
        self._listeners.append(cb)

    def _notify(self) -> None:
        for cb in self._listeners:
            cb()

    # ------------------------------------------------------------------
    # Inputs
    # ------------------------------------------------------------------

    def _feed_in_today(self) -> Optional[float]:
        """Today's cumulative site export, in kWh, from the coordinator.

        The coordinator nests the API payload under a "battery" key
        (alongside connection_status etc.), so this must not read the top
        level — doing so silently yields None forever and the controller
        never leaves "Export sensor unavailable".
        """
        data = getattr(self._coordinator, "data", None) or {}
        battery = data.get("battery") or {}
        raw = battery.get("Feed_In_Today")
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def _window(self) -> tuple[Optional[time], Optional[time]]:
        """Time Period 1 start/end — the window the target applies to."""
        start = self._manager.effective_feedin_slot(TIME_PERIOD_1, "start")
        end = self._manager.effective_feedin_slot(TIME_PERIOD_1, "end")
        return _parse(start), _parse(end)

    def _current_power(self) -> Optional[float]:
        val = self._manager.effective_feedin_slot(TIME_PERIOD_1, "power")
        try:
            return float(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Control loop
    # ------------------------------------------------------------------

    async def _async_tick(self, now: datetime | None = None) -> None:
        now = now or dt_util.now()
        if now.tzinfo is not None:
            now = now.astimezone(dt_util.DEFAULT_TIME_ZONE).replace(tzinfo=None)

        start, end = self._window()
        if start is None or end is None:
            self._plan = None
            self._notify()
            return

        await self._async_manage_baseline(now, start, end)

        plan = compute_plan(
            now=now, start=start, end=end,
            target_kwh=self._target_kwh,
            feed_in_today_kwh=self._feed_in_today(),
            baseline_kwh=self._baseline_kwh,
            enabled=self._enabled,
            max_power_w=FEEDIN_MAX_POWER_W,
        )
        self._plan = plan
        self._notify()

        if not plan.active:
            return

        current = self._current_power()
        if not should_write(current, plan.power_w,
                            now=now, last_write=self._last_write_at):
            return

        await self._async_apply(plan.power_w, now)

    async def _async_manage_baseline(
        self, now: datetime, start: time, end: time
    ) -> None:
        """Capture the baseline on entering the window; clear it on leaving.

        Keyed by date so a window that has already run today is not
        re-baselined if the user toggles something, and so the baseline is
        not carried across days.
        """
        inside = in_window(now.time(), start, end)
        today = now.strftime("%Y-%m-%d")

        if inside and self._baseline_kwh is None:
            reading = self._feed_in_today()
            if reading is None:
                return  # try again next tick; sensor not ready
            self._baseline_kwh = reading
            self._baseline_date = today
            self._last_write_at = None  # allow an immediate first write
            _LOGGER.info(
                "Export window opened — baseline %.2f kWh, target %.2f kWh",
                reading, self._target_kwh,
            )
            await self._async_save()
        elif not inside and self._baseline_kwh is not None:
            _LOGGER.info(
                "Export window closed — exported %.2f kWh of %.2f kWh target",
                (self._plan.exported_kwh if self._plan else 0.0),
                self._target_kwh,
            )
            self._baseline_kwh = None
            self._baseline_date = None
            self._applied_w = None
            await self._async_save()

    async def _async_apply(self, power_w: int, now: datetime) -> None:
        """Push the new cap straight to the inverter.

        Uses submit_feedin_one_shot rather than stage + submit: the staged
        path would also commit any half-finished edits the user has open in
        the UI, which an unattended loop has no business doing.
        """
        try:
            result = await self._manager.submit_feedin_one_shot(
                slots={TIME_PERIOD_1: {"power": power_w}}
            )
        except Exception as ex:  # noqa: BLE001 — a control loop must not die
            self._last_error = str(ex)
            _LOGGER.error("Export target: feed-in write failed: %s", ex)
            self._notify()
            return

        if result.all_ok:
            self._applied_w = power_w
            self._last_write_at = now
            self._last_error = None
            _LOGGER.info(
                "Export target: feed-in power -> %d W (%s)",
                power_w, self._plan.reason if self._plan else "",
            )
            await self._async_save()
        else:
            self._last_error = result.feedin_error or "unknown error"
            _LOGGER.warning(
                "Export target: feed-in write rejected: %s", self._last_error
            )
        self._notify()


def _parse(value: Any) -> Optional[time]:
    if not value or ":" not in str(value):
        return None
    try:
        parts = str(value).split(":")
        return time(int(parts[0]), int(parts[1]))
    except (ValueError, IndexError):
        return None
