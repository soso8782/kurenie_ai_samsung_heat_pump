<p align="center"><img src="brand/icon.png" width="120" alt="Logo"></p>

# Samsung Heat Pump AI Control for Home Assistant

[![English](https://img.shields.io/badge/lang-English-blue.svg)](README.md) [![Slovenčina](https://img.shields.io/badge/jazyk-Sloven%C4%8Dina-green.svg)](README.sk.md)

> **The integration is maintained in a separate repository:**
> **[samsung_heat_pump-ai-control-home-assistant-integration](https://github.com/soso8782/samsung_heat_pump-ai-control-home-assistant-integration)**.
> Add **its** URL to HACS (not the URL of this repository). The copy in this folder may be out of date.

The integration connects Home Assistant to the **Samsung Heat Pump AI Control** web app (an agent for Samsung heat pumps) and creates one device
with entities for monitoring and control. Data is read locally (`local_polling`), by default every 30 s.

The integration does not provide consumption and COP for the Energy dashboard: those stay on the MQTT sensors from `ha_pub.py`.

## Installation

**Via HACS (custom repository):**
1. HACS, menu at the top right, Custom repositories, enter the repository URL `https://github.com/soso8782/samsung_heat_pump-ai-control-home-assistant-integration`, category Integration.
2. Install "Samsung Heat Pump AI Control" and restart Home Assistant.

**Manually:** copy the `custom_components/samsung_heat_pump_ai_control` folder to `config/custom_components/` and restart Home Assistant.

## Setup

1. In the Samsung Heat Pump AI Control app open Settings, the **Home Assistant integration** card, and click **Create key**.
   The key is shown only once, so copy it. The old key stops working once a new one is created, and **Revoke key** disconnects the integration.
2. In Home Assistant: Settings, Devices & services, Add integration, **Samsung Heat Pump AI Control**.
3. Enter the app address (e.g. `http://10.10.40.30`) and the key.
4. You can change the refresh interval (15 to 300 s) with the Configure button on the integration.
   If you revoke or replace the key, Home Assistant offers re-authentication (reauth).

## Entities

Device "Samsung Heat Pump AI Control" (manufacturer Samsung).

| Type | Entity |
|---|---|
| sensor | Outdoor temperature; temperature of each room (attributes `min_target`, `reference`); flow temperature, return temperature, target temperature from the heating curve, Delta T; compressor frequency; curve offset; DHW temperature (if there is DHW); error code; agent status (ok / pozor / problem); last agent assessment (full text in the `summary` attribute); alerts (count, list in the `alerts` attribute) |
| binary_sensor | Compressor running; defrost; DHW heating (if there is DHW); problem (alert or error code ≠ 0); connection of the ESP, the collector and Home Assistant |
| select | Agent mode (off / advises / controls) |
| number | Target room temperature (step 0.5, range given by the app) |
| button | Run agent now; start DHW heating (if there is DHW) |

After each action the data is refreshed immediately. If the app rejects an action (e.g. DHW heating cannot be started now), Home Assistant shows its message.

## Troubleshooting

- "Failed to connect": check the address and that the app is running; the integration uses the address from the app directly (http or https).
- "Key is invalid": create a new key in the app and enter it again.
- Detailed logs: `logger: logs: custom_components.samsung_heat_pump_ai_control: debug`.
