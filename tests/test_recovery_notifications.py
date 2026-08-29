"""Tests for recovery notification behaviour.

The integration runs an auto-reconnect every day at 03:30 (default
``auto_reconnect_time``). That is routine maintenance on a healthy
connection, but it shared a code path with genuine failure recovery, so it
ended every night by raising:

    ByteWatt Recovery Success
    ByteWatt integration successfully reconnected to the API

— a daily notification whose content is "nothing was wrong". Besides being
noise, it trains users to dismiss ByteWatt notifications on sight, so the
ones that matter get ignored too.

Rules pinned here:
  * scheduled + success  -> NO notification (but stale ones are dismissed)
  * automatic + success  -> notify (a real fault genuinely cleared)
  * scheduled + FAILURE  -> notify (a silent broken reconnect is worse)
  * notify_on_recovery=False -> never notify

The coordinator imports the full HA runtime, so rather than construct one we
exercise the decision logic directly against the same conditions the source
uses. ``test_source_matches_policy`` then asserts the source really is
written that way, so this file cannot silently drift from the code.

Run: .venv/bin/python -m pytest tests/test_recovery_notifications.py -v
"""
from __future__ import annotations

import os
import re

import pytest

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                    "custom_components", "bytewatt", "coordinator.py")


def should_notify_success(notify_on_recovery: bool, is_scheduled: bool) -> bool:
    """Mirrors the guard around the 'Recovery Success' notification."""
    return notify_on_recovery and not is_scheduled


def should_notify_failure(notify_on_recovery: bool, is_scheduled: bool) -> bool:
    """Failures always notify when notifications are enabled."""
    return notify_on_recovery


@pytest.mark.parametrize(
    "notify,scheduled,expected,why",
    [
        (True, True, False,
         "daily 03:30 reconnect on a healthy link must stay silent"),
        (True, False, True,
         "recovery from a real fault should tell the user it cleared"),
        (False, False, False, "notifications disabled"),
        (False, True, False, "notifications disabled"),
    ],
)
def test_success_notification_policy(notify, scheduled, expected, why):
    assert should_notify_success(notify, scheduled) is expected, why


@pytest.mark.parametrize("scheduled", [True, False])
def test_failures_always_notify(scheduled):
    """A scheduled reconnect that FAILS must still raise a notification.

    Silencing the scheduled path wholesale would hide a genuinely broken
    connection every night — strictly worse than the original noise.
    """
    assert should_notify_failure(True, scheduled) is True


def test_scheduled_success_still_dismisses_stale_notification():
    """Routine reconnect clears leftover error notifications without adding one.

    So if a real failure notified overnight and the 03:30 run fixes it, the
    user sees the notification disappear rather than a new one appear.
    """
    src = open(_SRC).read()
    block = src[src.index("if recovered:"):src.index("Refresh \"completed\"")]
    dismiss_pos = block.index("async_dismiss")
    guard_pos = block.index("if not is_scheduled:")
    assert dismiss_pos < guard_pos, (
        "async_dismiss must run for scheduled runs too — it should sit BEFORE "
        "the 'if not is_scheduled' guard, not inside it"
    )


def test_source_matches_policy():
    """Guard the real source, so this test file can't drift from the code."""
    src = open(_SRC).read()

    block = src[src.index("if recovered:"):src.index("Refresh \"completed\"")]
    assert "ByteWatt Recovery Success" in block
    assert "if not is_scheduled:" in block, (
        "the success notification must be guarded by 'if not is_scheduled'"
    )

    # The "attempting to reconnect" notice is equally noisy on a nightly run.
    attempt = re.search(
        r"if self\._notify_on_recovery[^\n]*:\n\s+async_create\(\s*\n\s+self\.hass,\s*\n\s+f\"ByteWatt integration is attempting",
        src)
    assert attempt is not None, "could not locate the 'attempting to reconnect' notification"
    assert "not is_scheduled" in attempt.group(0), (
        "the 'attempting to reconnect' notification must also skip scheduled runs"
    )

    # The failure notification must NOT have been silenced.
    fail_idx = src.index("ByteWatt Recovery Failed")
    fail_guard = src.rindex("if self._notify_on_recovery", 0, fail_idx)
    assert "is_scheduled" not in src[fail_guard:fail_idx], (
        "failure notifications must fire for scheduled runs too"
    )
