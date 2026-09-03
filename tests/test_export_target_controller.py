"""Integration tests for the ExportTargetController (HA layer).

These cover what the pure-logic tests cannot: reading the coordinator's
nested data, baseline capture/clear at the window edges, persistence across
a restart, and — most importantly — that the loop writes via
submit_feedin_one_shot() rather than the staged path.

homeassistant is stubbed faithfully enough to import export_target.py. The
stubs are installed inside the fixture and removed afterwards so they cannot
leak into other test modules.

Run: .venv/bin/python -m pytest tests/test_export_target_controller.py -v
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from datetime import datetime, time, timedelta

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "custom_components", "bytewatt", "export_target.py")

_FIXED_NOW = datetime(2026, 8, 28, 19, 0)


def _install_stubs():
    added = []
    patched = []

    ha = types.ModuleType("homeassistant")
    core = types.ModuleType("homeassistant.core")
    core.callback = lambda f: f
    core.HomeAssistant = type("HomeAssistant", (), {})

    ce = types.ModuleType("homeassistant.config_entries")
    ce.ConfigEntry = type("ConfigEntry", (), {})

    helpers = types.ModuleType("homeassistant.helpers")

    event = types.ModuleType("homeassistant.helpers.event")
    event.async_track_time_interval = lambda hass, cb, interval: (lambda: None)

    storage = types.ModuleType("homeassistant.helpers.storage")

    class _Store:
        """In-memory Store keyed by file name, so a 'restart' can reuse it."""
        _data: dict = {}

        def __init__(self, hass, version, key):
            self._key = key

        async def async_load(self):
            return _Store._data.get(self._key)

        async def async_save(self, data):
            _Store._data[self._key] = dict(data)

    storage.Store = _Store

    util = types.ModuleType("homeassistant.util")
    dt_mod = types.ModuleType("homeassistant.util.dt")
    dt_mod.now = lambda: _FIXED_NOW
    dt_mod.DEFAULT_TIME_ZONE = None
    util.dt = dt_mod

    mods = {
        "homeassistant": ha,
        "homeassistant.core": core,
        "homeassistant.config_entries": ce,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.event": event,
        "homeassistant.helpers.storage": storage,
        "homeassistant.util": util,
        "homeassistant.util.dt": dt_mod,
    }
    for name, mod in mods.items():
        existing = sys.modules.get(name)
        if existing is None:
            sys.modules[name] = mod
            added.append(name)
        else:
            for attr in dir(mod):
                if attr.startswith("__"):
                    continue
                if not hasattr(existing, attr):
                    setattr(existing, attr, getattr(mod, attr))
                    patched.append((existing, attr))
    return added, patched, _Store


@pytest.fixture
def mod():
    added, patched, store_cls = _install_stubs()
    store_cls._data.clear()

    pkg = "bw_et_stub"
    if pkg not in sys.modules:
        p = types.ModuleType(pkg)
        p.__path__ = [os.path.dirname(os.path.abspath(_SRC))]
        sys.modules[pkg] = p

        const = types.ModuleType(f"{pkg}.const")
        const.DOMAIN = "bytewatt"
        const.FEEDIN_MAX_POWER_W = 20000
        sys.modules[f"{pkg}.const"] = const

        logic_path = os.path.join(os.path.dirname(os.path.abspath(_SRC)),
                                  "export_target_logic.py")
        lspec = importlib.util.spec_from_file_location(
            f"{pkg}.export_target_logic", logic_path)
        lmod = importlib.util.module_from_spec(lspec)
        sys.modules[f"{pkg}.export_target_logic"] = lmod
        lspec.loader.exec_module(lmod)

    spec = importlib.util.spec_from_file_location(
        f"{pkg}.export_target", os.path.abspath(_SRC))
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)

    yield m

    for name in added:
        sys.modules.pop(name, None)
    for obj, attr in patched:
        try:
            delattr(obj, attr)
        except AttributeError:
            pass
    sys.modules.pop(spec.name, None)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------

class FakeCoordinator:
    def __init__(self, feed_in_today=None):
        self.data = {"battery": {"Feed_In_Today": feed_in_today},
                     "connection_status": "connected"}

    def set_feed_in(self, value):
        self.data["battery"]["Feed_In_Today"] = value


class FakeSubmitResult:
    def __init__(self, ok=True, error=None):
        self.all_ok = ok
        self.feedin_error = error


class FakeManager:
    """Records how the controller talks to SettingsManager."""

    def __init__(self, start="17:59", end="21:01", power=350):
        self._slot = {"start": start, "end": end, "power": power}
        self.one_shot_calls = []
        self.stage_calls = []
        self.submit_calls = 0
        self.result = FakeSubmitResult()

    def effective_feedin_slot(self, idx, field, default=None):
        return self._slot.get(field, default)

    async def submit_feedin_one_shot(self, top=None, slots=None):
        self.one_shot_calls.append({"top": top, "slots": slots})
        if self.result.all_ok and slots and 0 in slots:
            self._slot["power"] = slots[0]["power"]
        return self.result

    # Staged path — must NOT be used by the control loop.
    def stage_feedin_slot(self, idx, field, value):
        self.stage_calls.append((idx, field, value))

    async def submit(self):
        self.submit_calls += 1
        return FakeSubmitResult()


class FakeEntry:
    entry_id = "test_entry_1"


def make(mod, *, feed_in=100.0, target=12.0, enabled=True,
         start="17:59", end="21:01", power=350):
    coord = FakeCoordinator(feed_in)
    mgr = FakeManager(start=start, end=end, power=power)
    c = mod.ExportTargetController(None, FakeEntry(), coord, mgr)
    c._enabled = enabled
    c._target_kwh = target
    return c, coord, mgr


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reads_feed_in_from_nested_battery_key(mod):
    """Coordinator data is nested under 'battery'; a top-level read is None."""
    c, coord, _ = make(mod, feed_in=42.5)
    assert c._feed_in_today() == pytest.approx(42.5)

    coord.data = {"Feed_In_Today": 42.5}  # wrong shape
    assert c._feed_in_today() is None


@pytest.mark.asyncio
async def test_baseline_captured_on_entering_window(mod):
    c, _, _ = make(mod, feed_in=14.22)
    await c._async_tick(datetime(2026, 8, 28, 18, 0))
    assert c._baseline_kwh == pytest.approx(14.22)
    # Pre-window solar must not count toward the target.
    assert c.plan.exported_kwh == pytest.approx(0.0)
    assert c.plan.remaining_kwh == pytest.approx(12.0)


@pytest.mark.asyncio
async def test_baseline_cleared_after_window(mod):
    c, coord, _ = make(mod, feed_in=14.22)
    await c._async_tick(datetime(2026, 8, 28, 18, 0))
    assert c._baseline_kwh is not None
    await c._async_tick(datetime(2026, 8, 28, 21, 30))
    assert c._baseline_kwh is None


@pytest.mark.asyncio
async def test_progress_measured_from_baseline(mod):
    c, coord, _ = make(mod, feed_in=10.0, target=12.0)
    await c._async_tick(datetime(2026, 8, 28, 18, 0))
    coord.set_feed_in(15.0)          # 5 kWh exported in-window
    await c._async_tick(datetime(2026, 8, 28, 19, 30))
    assert c.plan.exported_kwh == pytest.approx(5.0)
    assert c.plan.remaining_kwh == pytest.approx(7.0)


@pytest.mark.asyncio
async def test_uses_one_shot_not_the_staged_path(mod):
    """The loop must never commit the user's staged UI drafts."""
    c, _, mgr = make(mod, feed_in=10.0)
    await c._async_tick(datetime(2026, 8, 28, 18, 0))

    assert mgr.one_shot_calls, "expected a direct one-shot write"
    assert mgr.stage_calls == [], "must not stage into the pending store"
    assert mgr.submit_calls == 0, "must not submit the user's pending drafts"

    call = mgr.one_shot_calls[0]
    assert call["top"] is None
    assert set(call["slots"].keys()) == {0}, "only Time Period 1"
    assert call["slots"][0]["power"] % 100 == 0


@pytest.mark.asyncio
async def test_rate_limited_to_one_write_per_30_min(mod):
    c, coord, mgr = make(mod, feed_in=10.0, target=12.0)
    await c._async_tick(datetime(2026, 8, 28, 18, 0))
    first = len(mgr.one_shot_calls)

    # Ticks at 5-minute intervals for 25 minutes -> no further writes.
    for m in (5, 10, 15, 20, 25):
        coord.set_feed_in(10.0 + m * 0.01)
        await c._async_tick(datetime(2026, 8, 28, 18, m))
    assert len(mgr.one_shot_calls) == first, "wrote inside the 30-minute window"

    # Past 30 minutes, a real change is allowed through.
    coord.set_feed_in(10.2)
    await c._async_tick(datetime(2026, 8, 28, 18, 35))
    assert len(mgr.one_shot_calls) > first


@pytest.mark.asyncio
async def test_no_writes_outside_the_window(mod):
    c, _, mgr = make(mod, feed_in=10.0)
    await c._async_tick(datetime(2026, 8, 28, 12, 0))
    await c._async_tick(datetime(2026, 8, 28, 23, 0))
    assert mgr.one_shot_calls == []


@pytest.mark.asyncio
async def test_disabled_does_not_write(mod):
    c, _, mgr = make(mod, feed_in=10.0, enabled=False)
    await c._async_tick(datetime(2026, 8, 28, 19, 0))
    assert mgr.one_shot_calls == []
    assert c.plan.reason == "Disabled"


@pytest.mark.asyncio
async def test_zero_target_does_not_write(mod):
    c, _, mgr = make(mod, feed_in=10.0, target=0.0)
    await c._async_tick(datetime(2026, 8, 28, 19, 0))
    assert mgr.one_shot_calls == []


@pytest.mark.asyncio
async def test_missing_sensor_is_survivable(mod):
    """No export reading -> stay idle rather than raise or write garbage."""
    c, _, mgr = make(mod, feed_in=None)
    await c._async_tick(datetime(2026, 8, 28, 19, 0))
    assert mgr.one_shot_calls == []
    assert c._baseline_kwh is None


@pytest.mark.asyncio
async def test_write_failure_is_recorded_not_raised(mod):
    """A rejected write must not kill the loop."""
    c, _, mgr = make(mod, feed_in=10.0)
    mgr.result = FakeSubmitResult(ok=False, error="rate limited")
    await c._async_tick(datetime(2026, 8, 28, 18, 0))
    assert c.last_error == "rate limited"
    # And a later tick still tries again.
    mgr.result = FakeSubmitResult(ok=True)
    await c._async_tick(datetime(2026, 8, 28, 18, 40))
    assert c.last_error is None


@pytest.mark.asyncio
async def test_exception_during_write_is_contained(mod):
    c, _, mgr = make(mod, feed_in=10.0)

    async def boom(**kwargs):
        raise RuntimeError("network down")

    mgr.submit_feedin_one_shot = boom
    await c._async_tick(datetime(2026, 8, 28, 18, 0))   # must not raise
    assert "network down" in (c.last_error or "")


@pytest.mark.asyncio
async def test_baseline_survives_a_restart(mod):
    """A restart mid-window must resume, not re-target the full amount."""
    c1, coord1, _ = make(mod, feed_in=10.0, target=12.0)
    await c1._async_tick(datetime(2026, 8, 28, 18, 0))
    assert c1._baseline_kwh == pytest.approx(10.0)

    # New controller instance, same storage key = a restart.
    c2, coord2, _ = make(mod, feed_in=16.0, target=12.0)
    await c2._async_load()
    await c2._async_tick(datetime(2026, 8, 28, 19, 30))

    assert c2._baseline_kwh == pytest.approx(10.0), "baseline was lost"
    assert c2.plan.exported_kwh == pytest.approx(6.0)
    assert c2.plan.remaining_kwh == pytest.approx(6.0)


@pytest.mark.asyncio
async def test_target_met_stops_immediately(mod):
    """Reaching the target commands 0 W without waiting for the rate limit."""
    c, coord, mgr = make(mod, feed_in=10.0, target=5.0)
    await c._async_tick(datetime(2026, 8, 28, 18, 0))
    coord.set_feed_in(15.5)          # target exceeded
    await c._async_tick(datetime(2026, 8, 28, 18, 10))   # inside 30 min

    assert c.plan.complete
    assert mgr.one_shot_calls[-1]["slots"][0]["power"] == 0


@pytest.mark.asyncio
async def test_missing_window_times_are_survivable(mod):
    c, _, mgr = make(mod, feed_in=10.0, start="", end="")
    await c._async_tick(datetime(2026, 8, 28, 19, 0))
    assert mgr.one_shot_calls == []
    assert c.plan is None


@pytest.mark.asyncio
async def test_disabling_clears_the_baseline(mod):
    c, _, _ = make(mod, feed_in=10.0)
    await c._async_tick(datetime(2026, 8, 28, 18, 0))
    assert c._baseline_kwh is not None
    await c.async_set_enabled(False)
    assert c._baseline_kwh is None
