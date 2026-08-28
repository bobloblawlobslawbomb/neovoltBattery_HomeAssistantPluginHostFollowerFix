"""Regression tests for the host/follower SoC fix.

The Neovolt cloud, queried with ``sysSn=All``, returns a CAPACITY-WEIGHTED
ENERGY POOL across every inverter on the account — not an average of
percentages:

    soc_All = (soc_host * cobat_host + soc_follower * cobat_follower)
              / (cobat_host + cobat_follower)

On this account ``getCustomMenuEssList`` reports two inverters:

    host      cobat = 50.40 kWh   <- the real, usable bank
    follower  cobat = 30.24 kWh   <- phantom capacity, reads soc = 0.0 %

Because the follower advertises capacity it does not have and sits at 0 %,
it drags the pooled figure down. Verified live against the API:

    host 63.90 %, follower 0.0 %
    -> (63.90*50.40 + 0.0*30.24) / 80.64 = 39.94 %   (API returned 39.94)

and the mobile app showed the HOST value, not the pooled one.

IMPORTANT: the scale factor is NOT constant. It equals
cobat_host/(cobat_host+cobat_follower) = 50.40/80.64 = 0.625 only while the
follower reads 0 %. Any hardcoded 1.6x correction would break as soon as
that inverter reports a non-zero SoC — hence the fix scopes the request to
the host serial rather than rescaling.

Only ``soc`` is distorted. Power fields (pbat/ppv/pload/pgrid) SUM across
inverters, and the follower contributes 0 W, so they read identically in
both scopes — confirmed live (1505 W under both All and the host serial).

Note on sign convention: ``pbat`` is POSITIVE when DISCHARGING (confirmed
against the app on a live discharge). It is passed through unmodified;
changing it would break existing automations and energy dashboards.

Run: .venv/bin/python -m pytest tests/test_host_follower.py -v
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Stub the Home Assistant modules neovolt_client imports at module scope, so
# the client can be loaded without HA core installed.
# ---------------------------------------------------------------------------


def _install_ha_stubs() -> None:
    if "homeassistant" in sys.modules:
        return

    ha = types.ModuleType("homeassistant")
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})

    helpers = types.ModuleType("homeassistant.helpers")
    aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
    aiohttp_client.async_get_clientsession = lambda hass: None

    util = types.ModuleType("homeassistant.util")
    dt_mod = types.ModuleType("homeassistant.util.dt")
    import datetime as _datetime

    dt_mod.now = lambda: _datetime.datetime(2026, 8, 28, 12, 0, 0)
    util.dt = dt_mod

    for name, mod in {
        "homeassistant": ha,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.aiohttp_client": aiohttp_client,
        "homeassistant.util": util,
        "homeassistant.util.dt": dt_mod,
    }.items():
        sys.modules[name] = mod


_install_ha_stubs()

_HERE = os.path.dirname(os.path.abspath(__file__))
_CLIENT_PATH = os.path.join(
    _HERE, "..", "custom_components", "bytewatt", "api", "neovolt_client.py"
)


def _load_client_module():
    """Load neovolt_client.py directly, bypassing the package __init__."""
    pkg_root = os.path.abspath(os.path.join(_HERE, ".."))
    if pkg_root not in sys.path:
        sys.path.insert(0, pkg_root)

    # neovolt_client does `from .neovolt_auth import ...` — provide the parent
    # package and the auth module so the relative import resolves.
    pkg_name = "bw_api_stub"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [os.path.dirname(os.path.abspath(_CLIENT_PATH))]
        sys.modules[pkg_name] = pkg

        auth = types.ModuleType(f"{pkg_name}.neovolt_auth")
        auth.EncryptionError = type("EncryptionError", (Exception,), {})
        auth.encrypt_password = lambda pw, user: "encrypted"
        sys.modules[f"{pkg_name}.neovolt_auth"] = auth

    spec = importlib.util.spec_from_file_location(
        f"{pkg_name}.neovolt_client", os.path.abspath(_CLIENT_PATH)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


client_mod = _load_client_module()


# ---------------------------------------------------------------------------
# Minimal fake aiohttp session that records every request and replays canned
# responses keyed by URL substring.
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._payload = payload
        self.status = status
        self.content_type = "application/json"

    async def json(self, **_kw):
        return self._payload

    async def text(self):
        import json

        return json.dumps(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Records (url, params) for every GET and replays canned payloads."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.payloads: dict[str, dict] = {
            "getLastPowerData": {
                "code": 200,
                "data": {"soc": 61.6, "pbat": 3544, "ppv": 0, "pload": 1493},
            },
            "getEnergyStatistics": {"code": 200, "data": {"epvT": 10180.94}},
            "getSumDataForCustomer": {"code": 200, "data": {"epvtoday": 35.67}},
            "staticsByDay": {"code": 200, "data": {"epvtoday": 35.67}},
        }

    def get(self, url: str, params=None, headers=None, **_kw):
        self.calls.append((url, dict(params or {})))
        for key, payload in self.payloads.items():
            if key in url:
                return _FakeResponse(payload)
        return _FakeResponse({"code": 200, "data": {}})

    def params_for(self, endpoint: str) -> dict:
        for url, params in self.calls:
            if endpoint in url:
                return params
        raise AssertionError(f"{endpoint} was never requested. Calls: {self.calls}")


def _make_client(host_sys_sn: str = "", host_system_id: str = ""):
    c = client_mod.NeovoltClient.__new__(client_mod.NeovoltClient)
    c.hass = None
    c.username = "user@example.com"
    c.password = "pw"
    c.base_url = client_mod.DEFAULT_BASE_URL
    c.token = "fake-token"
    c.host_system_id = host_system_id
    c.host_sys_sn = host_sys_sn
    c.session = _FakeSession()
    return c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_realtime_scoped_to_host_serial():
    """The real-time frame must be requested for the HOST inverter only.

    This is the actual bug: with sysSn=All the server returns the mean SoC
    across inverters, so the host's 61.6 % came back as 44.25 %.
    """
    c = _make_client(host_sys_sn="HOSTSN123")
    await c.async_get_battery_data()

    params = c.session.params_for("getLastPowerData")
    assert params["sysSn"] == "HOSTSN123", (
        "Real-time data must be scoped to the host serial, otherwise SoC is "
        f"averaged across inverters. Got sysSn={params['sysSn']!r}"
    )
    assert params["sysSn"] != "All"


@pytest.mark.asyncio
async def test_statistics_endpoints_still_use_all():
    """Statistics are station-level totals — they must NOT be narrowed.

    Guards against an over-broad fix: scoping these to the host serial would
    silently drop any energy the follower inverter contributed.
    """
    c = _make_client(host_sys_sn="HOSTSN123")
    await c.async_get_battery_data()

    assert c.session.params_for("getEnergyStatistics")["sysSn"] == "All"
    assert c.session.params_for("getSumDataForCustomer")["sn"] == "All"


@pytest.mark.asyncio
async def test_falls_back_to_all_without_host():
    """Single-inverter installs (no host configured) keep the old behaviour."""
    c = _make_client(host_sys_sn="")
    await c.async_get_battery_data()

    assert c.session.params_for("getLastPowerData")["sysSn"] == "All"


@pytest.mark.asyncio
async def test_host_soc_is_not_the_pooled_value():
    """End-to-end: the SoC surfaced to HA is the host's, not the pooled one.

    Mirrors the live measurement exactly — host 63.90 % with a follower at
    0.0 % advertising 30.24 kWh of phantom capacity pooled to 39.94 %.
    """
    c = _make_client(host_sys_sn="HOSTSN123")
    c.session.payloads["getLastPowerData"] = {
        "code": 200,
        "data": {"soc": 63.90, "pbat": 1505},
    }
    data = await c.async_get_battery_data()

    assert data["soc"] == pytest.approx(63.90)
    assert data["soc"] != pytest.approx(39.94, abs=0.5), "SoC is still the pooled value"


def test_pooled_soc_is_capacity_weighted_not_a_mean():
    """Pin the aggregation model itself, so the root cause can't be re-lost.

    A previous diagnosis assumed sysSn=All returned the unweighted MEAN of
    the two inverters. That model is DISPROVED: it predicts 31.95 % where
    the API actually returned 39.94 %. This test documents the arithmetic
    that does hold, so nobody re-derives the wrong one.
    """
    soc_host, cap_host = 63.90, 50.40
    soc_foll, cap_foll = 0.0, 30.24
    api_returned = 39.94

    weighted = (soc_host * cap_host + soc_foll * cap_foll) / (cap_host + cap_foll)
    assert weighted == pytest.approx(api_returned, abs=0.01)

    unweighted_mean = (soc_host + soc_foll) / 2
    assert unweighted_mean == pytest.approx(31.95, abs=0.01)
    assert unweighted_mean != pytest.approx(api_returned, abs=1.0)

    # The 0.625 scale is an artefact of the follower sitting at 0 %, NOT a
    # constant. Once it charges, the ratio moves — so rescaling is invalid.
    assert cap_host / (cap_host + cap_foll) == pytest.approx(0.625, abs=1e-6)
    drifted = (soc_host * cap_host + 50.0 * cap_foll) / (cap_host + cap_foll)
    assert drifted / soc_host != pytest.approx(0.625, abs=0.05), (
        "scale factor must drift when the follower is non-zero"
    )
