"""Export-target entities: target (number), enable (switch), progress (sensor).

Thin wrappers over ExportTargetController. They hold no control logic — they
read its plan and forward user edits to it.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .export_target import ExportTargetController

_LOGGER = logging.getLogger(__name__)

# A day's worth of headroom; the practical figure is far lower, but a hard
# cap keeps a fat-fingered entry from demanding a 20 kW cap all evening.
MAX_TARGET_KWH = 200


class _ExportTargetBase:
    """Shared device info + controller subscription."""

    _attr_has_entity_name = False

    def __init__(self, controller: ExportTargetController,
                 config_entry: ConfigEntry) -> None:
        self._controller = controller
        self._config_entry = config_entry

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._config_entry.entry_id)},
            "name": "ByteWatt Battery System",
            "manufacturer": "ByteWatt",
            "model": "Battery Management System",
        }

    async def async_added_to_hass(self) -> None:
        self._controller.add_listener(self._on_update)

    @callback
    def _on_update(self) -> None:
        if self.hass is not None:
            self.async_write_ha_state()


class ByteWattExportTargetNumber(_ExportTargetBase, NumberEntity):
    """How many kWh to export during the feed-in window."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, controller, config_entry) -> None:
        super().__init__(controller, config_entry)
        self._attr_name = "Export Target"
        self._attr_unique_id = f"{config_entry.entry_id}_export_target"
        self._attr_icon = "mdi:transmission-tower-export"
        self._attr_native_min_value = 0
        self._attr_native_max_value = MAX_TARGET_KWH
        self._attr_native_step = 0.5
        self._attr_native_unit_of_measurement = "kWh"
        self._attr_device_class = NumberDeviceClass.ENERGY_STORAGE

    @property
    def native_value(self) -> float:
        return self._controller.target_kwh

    async def async_set_native_value(self, value: float) -> None:
        await self._controller.async_set_target(value)


class ByteWattExportTargetSwitch(_ExportTargetBase, SwitchEntity):
    """Master enable for the export-target controller."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, controller, config_entry) -> None:
        super().__init__(controller, config_entry)
        self._attr_name = "Export Target Control"
        self._attr_unique_id = f"{config_entry.entry_id}_export_target_enabled"
        self._attr_icon = "mdi:tune-vertical"

    @property
    def is_on(self) -> bool:
        return self._controller.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._controller.async_set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._controller.async_set_enabled(False)


class ByteWattExportTargetProgressSensor(_ExportTargetBase, SensorEntity):
    """Export achieved so far in the current window."""

    def __init__(self, controller, config_entry) -> None:
        super().__init__(controller, config_entry)
        self._attr_name = "Export Target Progress"
        self._attr_unique_id = f"{config_entry.entry_id}_export_target_progress"
        self._attr_icon = "mdi:progress-clock"
        self._attr_native_unit_of_measurement = "kWh"
        # Deliberately NOT a device_class/state_class energy sensor: the value
        # resets to 0 every window, which would corrupt long-term statistics.

    @property
    def native_value(self) -> Optional[float]:
        plan = self._controller.plan
        if plan is None:
            return None
        return round(plan.exported_kwh, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        plan = self._controller.plan
        if plan is None:
            return {"status": "Not configured"}
        attrs: dict[str, Any] = {
            "status": plan.reason,
            "active": plan.active,
            "complete": plan.complete,
            "target_kwh": round(plan.target_kwh, 2),
            "remaining_kwh": round(plan.remaining_kwh, 2),
            "remaining_hours": round(plan.remaining_hours, 2),
            "progress_pct": plan.progress_pct,
            "commanded_power_w": plan.power_w,
        }
        if self._controller.last_error:
            attrs["last_error"] = self._controller.last_error
        return attrs


# ---------------------------------------------------------------------------
# Platform setup helpers
# ---------------------------------------------------------------------------

def _controller(hass: HomeAssistant, entry: ConfigEntry):
    return hass.data[DOMAIN][entry.entry_id].get("export_target")


async def async_setup_number_entry(
    hass: HomeAssistant, config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    controller = _controller(hass, config_entry)
    if controller is not None:
        async_add_entities([ByteWattExportTargetNumber(controller, config_entry)])


async def async_setup_switch_entry(
    hass: HomeAssistant, config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    controller = _controller(hass, config_entry)
    if controller is not None:
        async_add_entities([ByteWattExportTargetSwitch(controller, config_entry)])


async def async_setup_sensor_entry(
    hass: HomeAssistant, config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    controller = _controller(hass, config_entry)
    if controller is not None:
        async_add_entities([
            ByteWattExportTargetProgressSensor(controller, config_entry)
        ])
