# Medisana BS444 — Home Assistant Custom Integration

A native Home Assistant integration for Medisana Bluetooth scales (BS444, BS430, BS440, BS410, BS550).  
No dedicated ESP32 required — works with any active Bluetooth proxy already present in your HA setup.

---

## Background

This integration is based on the ESPHome custom component
[bwynants/weegschaal](https://github.com/bwynants/weegschaal), which in turn builds on the
reverse-engineering work from the
[BS440](https://github.com/keptenkurk/BS440) project.

The original ESPHome component runs directly on an ESP32 and uses the `ble_client` component to
connect to the scale. The problem with combining it with `bluetooth_proxy` on the same device is
that `ble_client` and `bluetooth_proxy` share the same BLE stack — after a weighing session the
proxy becomes unreliable until the ESP32 is restarted.

This integration moves all scale logic into Home Assistant itself, using HA's built-in Bluetooth
stack and [Bleak](https://github.com/hbldh/bleak). The ESP32 only needs to run a standard active
Bluetooth proxy (`bluetooth_proxy: active: true`) — it can be shared with all other BLE devices
without interference.

---

## Supported Devices

| Model | Tested | Notes |
|---|---|---|
| BS444 | ✅ | Use `time_offset: true` |
| BS410 | — | Use `time_offset: true` |
| BS430 | — | |
| BS440 | — | |
| BS550 | — | |

---

## Requirements

- Home Assistant 2023.x or newer
- An **active** Bluetooth proxy (ESP32 running ESPHome with `bluetooth_proxy: active: true`)  
  OR a native Bluetooth adapter on the HA host
- HACS or manual installation

---

## Installation

### Manual

1. Copy the `medisana_bs444` folder into your HA `config/custom_components/` directory.
2. Restart Home Assistant.
3. Go to **Settings → Integrations → Add Integration** and search for **Medisana BS444**.

### HACS (Custom Repository)

1. In HACS, add this repository as a custom repository (Integration category).
2. Install **Medisana BS444**.
3. Restart Home Assistant.

---

## Configuration

When adding the integration you will be asked for:

| Field | Description |
|---|---|
| **MAC Address** | Bluetooth MAC address of your scale (e.g. `08:B8:D0:B6:0B:B5`) |
| **Time Offset** | Enable for BS410 and BS444 models (adjusts the scale's internal epoch) |

Home Assistant may auto-discover the scale when it is switched on and the service UUID is detected.

---

## ESPHome Proxy Configuration

Remove all scale-related components from your ESPHome device and keep only the proxy:

```yaml
esphome:
  name: btproxy

esp32:
  board: lolin32
  framework:
    type: esp-idf

esp32_ble_tracker:
  scan_parameters:
    active: true

bluetooth_proxy:
  active: true
```

---

## Entities

For each of the two supported users the following entities are created:

**Sensors**

| Entity | Unit |
|---|---|
| Weight | kg |
| BMI | — |
| Body Fat | % |
| Water (TBW) | % |
| Muscle | % |
| Bone Mass | kg |
| kcal | kcal |
| Age | years |
| Height | cm |

**Binary Sensors**

| Entity |
|---|
| Male |
| Female |
| High Activity |

---

## How It Works

The scale communicates over BLE using GATT indications on a proprietary Medisana service
(`000078b2-0000-1000-8000-00805f9b34fb`). When the scale is switched on it starts advertising.
HA detects the advertisement, connects via the active Bluetooth proxy, subscribes to three
characteristics (Person, Weight, Body), and sends a timestamped command to trigger the data dump.
The scale responds with up to 30 historical measurements per user. The integration keeps the most
recent entry per user, publishes all sensor values, and disconnects.

---

## Credits

- **Protocol reverse engineering:** [keptenkurk/BS440](https://github.com/keptenkurk/BS440)
- **ESPHome component:** [bwynants/weegschaal](https://github.com/bwynants/weegschaal)
- **This HA integration** was designed and implemented by
  [Claude Sonnet 4.6](https://www.anthropic.com/claude) (Anthropic) —
  the entire port from ESPHome C++ to a native HA Python integration was written by the AI,
  including protocol analysis, packet parsing, BLE session management, coordinator pattern,
  entity definitions, config flow, and this README.
