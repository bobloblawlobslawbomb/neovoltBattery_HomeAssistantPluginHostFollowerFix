"""Pytest configuration — makes ``custom_components`` importable.

These tests exercise the parts of the integration that have no Home
Assistant *runtime* dependency (data models, validators, manager state).
They DO need ``pycryptodome`` (the integration imports it eagerly) and
the ``homeassistant`` core (for SettingsManager's dispatcher import).
Both are listed in requirements_test.txt; CI installs them.

Individual test modules call ``pytest.importorskip`` so a bare dev
sandbox without those deps gets clean skips, not collection errors.

NOTE on stubs: some modules (test_host_follower, test_options_flow) inject
FAKE ``homeassistant.*`` modules into sys.modules so they can import the
client / config flow without HA installed. Those fakes are deliberately
partial. ``REAL_HA`` below records whether a genuine Home Assistant was
importable BEFORE any stubbing, so modules that need the real thing can
skip instead of tripping over a stub.
"""
from __future__ import annotations

import importlib.util
import os
import sys

# Make ``custom_components.bytewatt`` importable from the repo root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Probe for a REAL homeassistant before any test module can stub it.
# find_spec doesn't execute the package, so this is cheap and side-effect free.
try:
    REAL_HA = importlib.util.find_spec("homeassistant.components") is not None
except (ImportError, AttributeError, ValueError):
    REAL_HA = False
