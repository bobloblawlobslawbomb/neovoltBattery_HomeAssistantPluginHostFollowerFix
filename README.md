# Byte-Watt Battery Monitor (Host/Follower Fix)

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)

Monitor and control a Byte-Watt / Neovolt battery system from Home Assistant.

> **This is a fork** of
> [candreacchio/neovoltBattery_HomeAssistantPlugin](https://github.com/candreacchio/neovoltBattery_HomeAssistantPlugin)
> that fixes **understated battery State of Charge on multi-inverter
> (host/follower) systems**. Upstream requests real-time data with
> `sysSn=All`, which returns a capacity-weighted energy pool across every
> inverter on the account — so a second inverter reporting 0 % (or
> advertising capacity it does not have) drags the SoC well below its true
> value. This fork scopes the real-time request to the Host inverter,
> matching what the Neovolt mobile app shows. See
> [v1.2.0](../../releases/tag/v1.2.0) for the full analysis.
>
> It keeps the `bytewatt` domain, so it is a drop-in replacement: existing
> config entries, entity IDs and history carry over unchanged. Install this
> **or** upstream, not both.

Requires Home Assistant **2024.11.0** or later.

## Features

- **Correct SoC on multi-inverter systems** — real-time data is read from the
  Host inverter instead of a capacity-weighted pool across all inverters
- **Real-time monitoring** — SOC, grid / house / PV / battery power flows
- **Cumulative + today's energy** — solar generation, feed-in, grid import, charge / discharge
- **Battery control** — charge / discharge time windows, minimum SOC, charge cap,
  per-slot charge & discharge power, grid charging on/off, discharge time control on/off
- **Grid Feed-in Control** — enable/disable, cutoff SOC, Time Period 1 start/end/power
- **Staged-edit workflow** — UI changes accumulate in a *pending* store and are
  pushed to the inverter in one shot via the **Submit Settings** button (mirrors
  the portal's Save button and avoids the API's rate-limit failures on rapid
  sequential writes). A **Discard Pending Settings** button drops them.
- **Multi-inverter support** — pick which inverter is the Host during setup, change
  it later via Configure (no need to delete and re-add).
- **Automatic recovery** — heartbeat monitoring, circuit breaker, auto-reconnect.

## Installation

### HACS (recommended)

> If you already have the upstream **Byte-Watt Battery Monitor** installed,
> remove it from HACS first — both use the `bytewatt` domain and cannot be
> installed side by side. Removing it in HACS only deletes files; do **not**
> delete the integration from Settings → Devices & Services, or you will lose
> your credentials, entity IDs and history.

1. Install [HACS](https://hacs.xyz/) if you haven't already.
2. HACS → Integrations → ⋮ → Custom repositories → add this repo URL → Category: Integration.
3. Install **Byte-Watt Battery Monitor (Host/Follower Fix)** and restart Home Assistant.
4. Settings → Devices & Services → Add Integration → search for **Byte-Watt**.

### Manual

Copy `custom_components/bytewatt` into your Home Assistant `custom_components/`
directory, restart, then add the integration as above.

## Setup

You'll be asked for:

- **Username** (your Byte-Watt portal email)
- **Password**
- **Scan interval** — 30 s minimum (default 60 s)

If the account has more than one inverter, a second step asks you to pick the
**Host inverter**. Single-inverter accounts skip that step automatically.

To change which inverter is the Host later: Settings → Devices & Services →
Byte-Watt → ⋮ → Reconfigure.

## Entities

| Platform | Entities |
|---|---|
| `sensor` | 30+ sensors covering real-time power, today's energy, cumulative totals, environmental stats |
| `switch` | Grid Charging Battery, Battery Discharge Time Control, Grid Feed-in Function |
| `number` | Minimum SOC, Battery Charge Cap, Battery Charge Power, Battery Discharge Power, Grid Feed-in Cutoff SOC, Grid Feed-in Time1 Power |
| `time`   | Charge Start/End, Discharge Start/End, Grid Feed-in Time1 Start/End |
| `button` | **Submit Settings**, **Discard Pending Settings** |

### Submit/Discard workflow

Changing a switch / number / time entity **does not** immediately write to the
inverter. The change is held locally and shown on the entity. Press
**Submit Settings** to push everything pending in one transaction. Press
**Discard Pending Settings** to drop staged changes and revert entities to the
inverter's current state.

On a successful submit, a persistent notification confirms. On a failure, the
notification explains which batch failed and why, and the pending changes are
**preserved** so you can fix the issue and press Submit again.

## Services

The legacy "set this one thing" services still work — they stage the change
and submit immediately (no Submit button press needed for services):

- `bytewatt.set_minimum_soc` — set minimum battery SOC (1–100 %)
- `bytewatt.set_charge_cap` — set charge cap (1–100 %)
- `bytewatt.set_discharge_start_time` / `set_discharge_time` — discharge window
- `bytewatt.set_charge_start_time` / `set_charge_end_time` — charge window
- `bytewatt.update_battery_settings` — set any combination in one call

Grid Feed-in:

- `bytewatt.set_grid_feedin_enabled` — toggle Grid Feed-in Function on/off
- `bytewatt.set_grid_feedin_cutoff_soc` — set discharging cutoff SOC (0–100 %)
- `bytewatt.update_grid_feedin_slot` — set start/end/power for slot 1–6

Maintenance:

- `bytewatt.force_reconnect` — drop the session and re-authenticate
- `bytewatt.health_check` — run network + auth + API diagnostics
- `bytewatt.toggle_diagnostics` — verbose API logging on/off

All services accept an optional `entry_id` field. If you have a single
Byte-Watt account configured you can omit it; with multiple accounts it's
required (the call will tell you which entry_ids exist).

## Example automations

```yaml
automation:
  - alias: "Peak — discharge"
    trigger:
      platform: state
      entity_id: sensor.electricity_price_tier
      to: 'peak'
    action:
      service: bytewatt.update_battery_settings
      data:
        start_discharge: "17:00"
        end_discharge: "22:00"
        minimum_soc: 20

  - alias: "Off-peak — charge"
    trigger:
      platform: state
      entity_id: sensor.electricity_price_tier
      to: 'off_peak'
    action:
      service: bytewatt.update_battery_settings
      data:
        start_charge: "01:00"
        end_charge: "05:00"

  - alias: "Daytime — enable grid feed-in"
    trigger:
      platform: time
      at: "09:00:00"
    action:
      service: bytewatt.set_grid_feedin_enabled
      data:
        feedin_enabled: true
```

## Configuration options

After install, Settings → Devices & Services → Byte-Watt → Configure:

- **Scan interval** (seconds) — minimum 30, default 60. Changes apply
  immediately (the integration reloads on options changes).

## Troubleshooting

- **A repair issue says "Host inverter not configured"** — you have more than
  one inverter on the account and no Host has been selected. Reconfigure
  (Settings → Devices & Services → Byte-Watt → ⋮ → Reconfigure) and pick one.
- **Submit button shows partial failure** — the notification names which
  batch failed (battery or grid feed-in) and the error reason. Your unsaved
  changes are kept; fix and Submit again.
- **Entity shows "unavailable"** — the integration hasn't yet fetched the
  relevant settings from the API. Usually transient; check logs if it persists.
- **Cumulative totals briefly drop at midnight** — known timezone quirk of the
  API; the integration mitigates it by querying through tomorrow's date.

### Real-time field mapping

| API field | Sensor |
|---|---|
| `pgrid` | Grid Consumption (W) |
| `pload` | House Consumption (W) |
| `pbat` | Battery Power (W) |
| `ppv` | PV Power (W) |
| `soc` | Battery Percentage (%) |
| `epvT` | Total Solar Generation (kWh) |
| `eout` | Total Feed In (kWh) |
| `echarge` | Total Battery Charge (kWh) |
| `edischarge` | Total Battery Discharge (kWh) |
| `epv2load` | PV Power to House (kWh) |
| `epvcharge` | PV Charging Battery (kWh) |
| `eload` | Total House Consumption (kWh) |
| `egridCharge` | Grid Based Battery Charge (kWh) |
| `einput` | Grid Power Consumption (kWh) |

Enable debug logging in `configuration.yaml` to see all fields the API returns:

```yaml
logger:
  default: info
  logs:
    custom_components.bytewatt: debug
```

## Support

Open an issue at https://github.com/candreacchio/neovoltBattery_HomeAssistantPlugin/issues.

## Credits

Originally built with the Home Assistant community and Claude AI. Subsequent
contributors are credited in the commit history.
