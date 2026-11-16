# Installation profile (`profil.json`)

[![English](https://img.shields.io/badge/lang-English-blue.svg)](PROFIL.md) [![Slovenčina](https://img.shields.io/badge/jazyk-Sloven%C4%8Dina-green.svg)](PROFIL.sk.md)

Everything specific to a particular house and heat pump (model, entity names in the ESP, rooms, agent limits, history sources
from Home Assistant) is in a single file, **`profil.json`**, in the app folder. It is managed by the `profil.py` module
and the web wizard (Settings → **Device and home**).

- **Without `profil.json`** the default profile applies = the original installation (Samsung AE120JXEDGH, radiators, 7 rooms)
  and the app behaves exactly like version 1.x.
- The file is not in git (it exists only on the server). Template: [`profil.example.json`](../profil.example.json).
- Missing items are filled in from the default profile. An invalid value is rejected on save with an error message.
- **The services must be restarted after a profile change** (`systemctl restart kurenie-agent kurenie-collector kurenie-web`,
  or the "Restart services" button in the wizard) – the modules load their maps at startup.

## Profile items

### `verzia`
Version number of the format (currently `1`). Do not change it.

### `zariadenie`
| Key | Meaning |
|---|---|
| `model` | Model ID = file name in `modely/` without `.json` (e.g. `AE120JXEDGH`). Empty/unknown = no manufacturer table (see below). Allowed characters: letters, digits, `- _ .` |
| `nazov` | Display name (up to 80 characters), also goes into the agent prompt |
| `typ` | `split`, `mono` or `hydrosplit` |
| `vykon_kw` | Rated capacity, 2–30 kW |
| `fazy` | `1` or `3` |
| `chladivo` | Text (R410A, R32, R290 …), informational only |
| `kurenie` | `radiatory`, `podlahove` or `kombinovane` (radiators, underfloor, combined) – determines the **default agent limits** |
| `tuv` | `true`/`false` – whether a hot water (DHW) tank is connected. With `false` the DHW logic is switched off |

### `esp`
Entity names as ESPHome publishes them through `web_server` (the app looks them up by exact name).
**An empty string = the entity is not available** (a feature that needs it is silently disabled). A sample ESPHome YAML
with these names: [`esphome/samsung-ehs.yaml`](../esphome/samsung-ehs.yaml).

`esp.entity` – canonical keys:

| Key | Meaning | Required |
|---|---|---|
| `vonku` | outdoor temperature | yes |
| `tw2` | water flow temperature | yes |
| `tw1` | return temperature | yes |
| `freq` | compressor frequency | yes |
| `wl` | target weather-compensation water temperature (WL) | yes |
| `posun` | weather compensation curve offset | yes |
| `sledovana` | room temperature monitored by the firmware (reference room) | recommended |
| `ciel_akt` | current target room temperature | no |
| `ciel_izby` | adjustable target room temperature | no |
| `dt` | Delta T (Tw2 − Tw1) | no |
| `prikon` | heat pump power input (W) | no |
| `tuv`, `tuv_ciel` | DHW temperature and target | only with DHW |
| `pwm` | circulation pump speed | no |
| `chyba` | error code | no |
| `kwh_den` | daily heat pump consumption (kWh) | no |
| `flow` | water flow rate (l/min) | no |
| `ventil` | 3‑way valve (heating direction) | no |
| `odmrazovanie` | defrost | no |
| `termostat` | heat demand | no |

`esp.nastavenia` – settings the agent may change or that are monitored: `fsv2011`, `fsv2012`, `fsv2021`, `fsv2022`,
`fsv2031`, `fsv2032`, `fsv4013`, `max_posun`, `max_posun_zaporny`, `zosilnenie`. (Required for the agent: `fsv2031`, `fsv2032`.)

`esp.rest_extra` – a list of additional entities the collector reads via REST (e.g. radiator heads).

### `izby`
A list of rooms (at least one). Each room:

| Key | Meaning |
|---|---|
| `kluc` | short identifier: lowercase letters without diacritics `a-z0-9_`, starts with a letter, max 21 characters, **unique**, not reserved (`ts`, `vonku`, `tw2`, `cop` … – see the `RESERVED` list in `profil.py`). It is a column name in `stats.db` – **do not change** it after the first deployment |
| `nazov` | display name |
| `esp` | name of the room temperature entity in the ESP (empty = HA only) |
| `ha` | list of `sensor.*` entities in Home Assistant (history, import) – up to 5, order = priority |
| `min` | the room's target minimum (°C, 16–26) or `null` (default 21) |
| `referencna` | **exactly one** room has `true` |

Columns for new rooms are added to `stats.db` automatically (`ALTER TABLE … ADD COLUMN`); they are never deleted.

### Reference room
The reference room is the room whose temperature the heat pump itself monitors (the `sledovana` entity in the ESP) and by which
the firmware shifts the weather compensation curve. The app uses it as a **comparison baseline**: temperature differences of the
other rooms are calculated against it, "hours below target" are counted by it, and it is referred to by its name in the
agent prompts. The reference room has no target minimum of its own (`min` is ignored – its target temperature is `ciel_izby`).
If the profile has more than one reference room, or none, validation returns an error (on load the first one is used).

### `ine_teploty`
Additional temperatures for monitoring only (not rooms used for control): `{"chodba": ["sensor.…"], …}`. The same rules apply to
the key as for rooms, and it must not match a room.

### `ha`
History sources from Home Assistant for import (`stats_backfill`): `vonku`, `tw2`, `tw1`, `wl`, `tuv` → a list of `entity_id`
(order = priority, an empty list = no import).

### `mantinely`
The agent's safety limits. Keys:

- `ciel_izby`: `[min, max]` of the target room temperature (absolute limits 16–26 °C).
- `fsv2031`, `fsv2032`, `fsv4013`, `max_posun`: `[min, max, max_step, hours_between_changes, rounding]`.

If a key is missing, the **default value for the heating type** (`zariadenie.kurenie`) is used:

| | radiators | underfloor | combined |
|---|---|---|---|
| FSV 2031 (water temperature at the coldest outdoor temperature) | 42–50 °C | 30–40 °C | 35–45 °C |
| FSV 2032 (water temperature at the warmest outdoor temperature) | 32–38 °C | 24–32 °C | 28–35 °C |
| FSV 4013 (heating off above outdoor temperature) | 17–24 °C | 15–22 °C | 16–23 °C |
| max. positive curve offset | 1–4 | 0.5–2 | 0.5–3 |

The maximum step is 1 °C (offset 0.5) and the minimum interval between changes is 24 h (offset 12 h).

The **absolute limits** are fixed and the profile can only **narrow** them, never widen them (protection of the unit and the
house; a value outside the limits is rejected on save):

| Parameter | absolute min–max |
|---|---|
| FSV 2031 | 25–55 °C |
| FSV 2032 | 20–45 °C |
| FSV 4013 | 10–35 °C |
| max. positive offset | 0–5 |
| target room temperature | 16–26 °C |

### `elektrina`
`ims_stat` (ID of the external statistic in HA) and `meter_ids` (up to 4 electricity meter entities). Empty = the environment
variables are used, or the feature is disabled.

## Model and COP without a table

The `modely/<id>.json` files contain the manufacturer's *Maximum Heating Capacity (Peak)* table: for the outdoor temperature (`ta`)
and the water flow temperature (`lw`), a pair `[HC kW, PI kW]` (`null` = outside the table range). The app uses it to calibrate
the flow rate and calculate COP. The `overene` field says whether the table has been verified on a real unit (in the library only
AE120JXEDGH is marked that way; the others were transcribed from the manufacturer's documentation – see the `zdroj` field in each file).

**If there is no table for your model** (the model is empty or the file does not exist):
- heat output and COP are calculated only if a flow rate is available from a sensor (`esp.entity.flow`) or from the configuration
  (`STATS_FLOW_LPM`, `STATS_FLOW_TUV_LPM` in `config.env`);
- if neither is available, heat output and COP are "unavailable" (no error, the other features work);
- you add your own model by copying one of the files from `modely/`, editing the numbers and the `id` (file name = `id`,
  `overene: false`) – use **only numbers from the manufacturer's documentation** (Technical Data Book) and state the source.

## Example: underfloor heating, 3 rooms, no DHW

```json
{
  "zariadenie": {"model": "", "nazov": "Samsung EHS Mono 6 kW", "typ": "mono", "vykon_kw": 6,
                 "fazy": 1, "chladivo": "R32", "kurenie": "podlahove", "tuv": false},
  "esp": {
    "entity": {"tuv": "", "tuv_ciel": "", "pwm": "", "ventil": "", "termostat": ""}
  },
  "izby": [
    {"kluc": "obyvacka", "nazov": "Obývačka", "esp": "Izba Obývačka", "referencna": true,
     "ha": ["sensor.obyvacka_teplota"]},
    {"kluc": "kuchyna", "nazov": "Kuchyňa", "esp": "Izba Kuchyňa", "min": 21.0, "ha": ["sensor.kuchyna_teplota"]},
    {"kluc": "spalna", "nazov": "Spálňa", "esp": "Izba Spálňa", "min": 19.0, "ha": ["sensor.spalna_teplota"]}
  ],
  "ine_teploty": {},
  "ha": {"vonku": ["sensor.vonkajsia_teplota"]},
  "mantinely": {"ciel_izby": [19.0, 24.0]}
}
```

What happens:
- `kurenie: podlahove` → limits FSV 2031 30–40 °C, FSV 2032 24–32 °C, offset 0.5–2 (the agent is told in its prompt that the
  house has underfloor heating and will not propose radiator water temperatures).
- `tuv: false` and empty `tuv`/`tuv_ciel` → the DHW tab is hidden and the DHW scheduler does not run.
- The living room (`obyvacka`) is the reference room: differences and hours below target are calculated against it.
- `model: ""` → no table; COP only if a flow rate is available from a sensor or from the configuration.
- Missing keys (`esp.nastavenia`, `elektrina`…) are filled in from the default profile – so the entity names in the ESP must
  match the defaults (see `esphome/samsung-ehs.yaml`), or you override them in the profile.

A more complete example (Mono 8 kW, underfloor heating, 4 rooms, no DHW): [`profil.example.json`](../profil.example.json).

## Wizard in the web UI

Settings → **Device and home** (admin only). One page with 4 sections:

1. **Device** – choose a model from the library (or "other/unknown"), unit type, capacity, phases, heating type (the default
   limits are shown after a change) and whether there is DHW.
2. **ESP entities** – for each canonical key a drop-down list of the entities found in the ESP (`ESP_URL`), pre-filled with an
   automatic suggestion based on the name, with the live value next to it. Required ones are marked.
3. **Rooms** – add/remove/reorder, key (generated from the name, editable only for a new room), name, ESP entity,
   HA sensors (chosen from the temperature sensors in HA), minimum and the "reference" switch (exactly one).
4. **Limits** – min/max for FSV 2031/2032/4013, the maximum offset and the target room temperature range, with the absolute limits.

Buttons: **Save** (errors are shown next to the fields), **Restore defaults** (deletes `profil.json` → the original installation)
and, after saving, **Restart services**. Without a restart the change does not take effect.
