"""Regression test for the options flow 500 error on HA 2025.12+.

Clicking the cog on the integration card returned:

    Config flow could not be loaded: 500 Internal Server Error

Cause: ``OptionsFlow.config_entry`` is a read-only PROPERTY in modern HA —
the base class sets it from the entry the flow was created for. The handler
had a custom ``__init__`` doing ``self.config_entry = config_entry``, which
was deprecated in 2024.11 (warning only) and started raising in 2025.12:

    AttributeError: property 'config_entry' of
    'ByteWattOptionsFlowHandler' object has no setter

The AttributeError is raised inside ``async_create_flow``, so the frontend
never gets a form and surfaces a bare HTTP 500.

These tests model the modern base class faithfully — ``config_entry`` as a
property with NO setter — so the old code fails here exactly as it does on a
real 2025.12+ install.

Run: .venv/bin/python -m pytest tests/test_options_flow.py -v
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

import pytest

# Attributes we grafted onto a sibling module's partial stub, so teardown can
# remove exactly those and leave the sibling's own stub as it found it.
patched: list[tuple[object, str]] = []


def _install_stubs() -> list[str]:
    """Stub homeassistant + voluptuous faithfully enough to import config_flow.

    Returns the list of sys.modules keys it added, so the caller can remove
    them again — leaving real (or absent) homeassistant modules untouched for
    every other test module in the run.
    """
    added: list[str] = []
    ha = types.ModuleType("homeassistant")
    core = types.ModuleType("homeassistant.core")
    core.callback = lambda f: f
    core.HomeAssistant = type("HomeAssistant", (), {})

    ce = types.ModuleType("homeassistant.config_entries")

    class ConfigEntry:
        def __init__(self, data=None, options=None, entry_id="e1"):
            self.data = data or {}
            self.options = options or {}
            self.entry_id = entry_id

    class OptionsFlow:
        """Mirrors modern HA: config_entry is a READ-ONLY property."""

        _config_entry = None

        @property
        def config_entry(self):
            return self._config_entry

        def async_show_form(self, **kw):
            return {"type": "form", **kw}

        def async_create_entry(self, **kw):
            return {"type": "create_entry", **kw}

    class ConfigFlow:
        def __init_subclass__(cls, domain=None, **kw):
            super().__init_subclass__(**kw)

        def async_show_form(self, **kw):
            return {"type": "form", **kw}

        def async_create_entry(self, **kw):
            return {"type": "create_entry", **kw}

        def async_abort(self, **kw):
            return {"type": "abort", **kw}

    ce.ConfigEntry = ConfigEntry
    ce.OptionsFlow = OptionsFlow
    ce.ConfigFlow = ConfigFlow

    helpers = types.ModuleType("homeassistant.helpers")
    sel = types.ModuleType("homeassistant.helpers.selector")

    class _S:
        def __init__(self, config=None):
            self.config = config

    sel.SelectSelector = _S
    sel.SelectSelectorConfig = lambda **kw: kw
    sel.SelectOptionDict = lambda **kw: dict(**kw)

    class _Mode:
        DROPDOWN = "dropdown"
        LIST = "list"

    sel.SelectSelectorMode = _Mode

    const = types.ModuleType("homeassistant.const")
    const.CONF_USERNAME = "username"
    const.CONF_PASSWORD = "password"
    const.CONF_SCAN_INTERVAL = "scan_interval"

    dr = types.ModuleType("homeassistant.helpers.aiohttp_client")
    dr.async_get_clientsession = lambda hass: None

    for name, mod in {
        "homeassistant": ha,
        "homeassistant.core": core,
        "homeassistant.config_entries": ce,
        "homeassistant.const": const,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.selector": sel,
        "homeassistant.helpers.aiohttp_client": dr,
    }.items():
        existing = sys.modules.get(name)
        if existing is None:
            sys.modules[name] = mod
            added.append(name)
        else:
            # A sibling test module may have installed its own partial stub
            # (e.g. homeassistant.core without `callback`). Fill in only the
            # attributes it lacks rather than clobbering it, and remember what
            # we added so teardown can undo exactly that.
            for attr in dir(mod):
                if attr.startswith("__"):
                    continue
                if not hasattr(existing, attr):
                    setattr(existing, attr, getattr(mod, attr))
                    patched.append((existing, attr))
    return added


def _remove_stubs(added: list[str]) -> None:
    for name in added:
        sys.modules.pop(name, None)
    for obj, attr in patched:
        try:
            delattr(obj, attr)
        except AttributeError:
            pass
    patched.clear()


_HERE = os.path.dirname(os.path.abspath(__file__))
_CF = os.path.join(_HERE, "..", "custom_components", "bytewatt", "config_flow.py")


@pytest.fixture(scope="module")
def cf():
    """Import config_flow.py in isolation, stubbing its intra-package imports.

    The homeassistant stubs are installed lazily HERE rather than at module
    import time, and removed afterwards, so they cannot leak into other test
    modules. test_settings_manager.py does a real `importorskip` on
    homeassistant and would otherwise pick up these fakes and fail to collect.
    """
    added = _install_stubs()
    pkg = "bw_cf_stub"
    if pkg not in sys.modules:
        p = types.ModuleType(pkg)
        p.__path__ = [os.path.dirname(os.path.abspath(_CF))]
        sys.modules[pkg] = p

        const_mod = types.ModuleType(f"{pkg}.const")
        const_mod.DOMAIN = "bytewatt"
        const_mod.CONF_HOST_SYSTEM_ID = "host_system_id"
        const_mod.CONF_HOST_SYS_SN = "host_sys_sn"
        const_mod.DEFAULT_SCAN_INTERVAL = 60
        const_mod.MIN_SCAN_INTERVAL = 30
        const_mod.CURRENT_ENTRY_VERSION = 3
        # const.py re-exports these from homeassistant.const; config_flow
        # imports them from .const, so the stub must provide them too.
        const_mod.CONF_USERNAME = "username"
        const_mod.CONF_PASSWORD = "password"
        const_mod.CONF_SCAN_INTERVAL = "scan_interval"
        const_mod.__file__ = os.path.join(
            os.path.dirname(os.path.abspath(_CF)), "const.py")
        sys.modules[f"{pkg}.const"] = const_mod

        client_mod = types.ModuleType(f"{pkg}.bytewatt_client")
        client_mod.ByteWattClient = type("ByteWattClient", (), {})
        sys.modules[f"{pkg}.bytewatt_client"] = client_mod

    pytest.importorskip("voluptuous")
    spec = importlib.util.spec_from_file_location(
        f"{pkg}.config_flow", os.path.abspath(_CF))
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    # Expose the stub ConfigEntry so tests don't import homeassistant directly.
    m._StubConfigEntry = sys.modules["homeassistant.config_entries"].ConfigEntry

    yield m

    # Remove our fakes so later test modules see a clean sys.modules.
    _remove_stubs(added)
    sys.modules.pop(spec.name, None)


def test_options_flow_constructs_without_error(cf):
    """The regression: building the handler must not raise AttributeError.

    Fails on the old code with:
        AttributeError: property 'config_entry' ... has no setter
    """
    entry = cf._StubConfigEntry(data={"username": "u"}, options={})
    handler = cf.ByteWattConfigFlow.async_get_options_flow(entry)
    assert handler is not None


def test_options_flow_handler_defines_no_init(cf):
    """Guard the actual fix: a custom __init__ is what reintroduces the bug."""
    assert "__init__" not in vars(cf.ByteWattOptionsFlowHandler), (
        "ByteWattOptionsFlowHandler must not define __init__ — assigning "
        "self.config_entry raises on HA 2025.12+ and breaks the cog with a 500"
    )


def test_assigning_config_entry_would_raise(cf):
    """Prove the stub really is read-only, so the tests above have teeth."""
    handler = cf.ByteWattOptionsFlowHandler()
    with pytest.raises(AttributeError):
        handler.config_entry = "anything"


@pytest.mark.asyncio
async def test_options_form_renders_with_inherited_entry(cf):
    """The form still builds, reading options via the INHERITED property."""
    handler = cf.ByteWattOptionsFlowHandler()
    # Base class populates the private attr; emulate that.
    handler._config_entry = cf._StubConfigEntry(
        data={}, options={"scan_interval": 120})

    result = await handler.async_step_init()
    assert result["type"] == "form"
    assert result["step_id"] == "init"


@pytest.mark.asyncio
async def test_options_submit_creates_entry(cf):
    handler = cf.ByteWattOptionsFlowHandler()
    result = await handler.async_step_init({"scan_interval": 90})
    assert result["type"] == "create_entry"
    assert result["data"] == {"scan_interval": 90}
