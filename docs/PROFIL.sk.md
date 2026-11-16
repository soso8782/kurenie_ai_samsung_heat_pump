# Profil inštalácie (`profil.json`)

[![English](https://img.shields.io/badge/lang-English-blue.svg)](PROFIL.md) [![Slovenčina](https://img.shields.io/badge/jazyk-Sloven%C4%8Dina-green.svg)](PROFIL.sk.md)

Všetko, čo je špecifické pre konkrétny dom a tepelné čerpadlo (model, názvy entít v ESP, izby, mantinely agenta, zdroje
histórie z Home Assistantu), je v jednom súbore **`profil.json`** v priečinku aplikácie. Spravuje ho modul `profil.py`
a webový sprievodca (Nastavenia → **Zariadenie a dom**).

- **Bez `profil.json`** platí predvolený profil = pôvodná inštalácia (Samsung AE120JXEDGH, radiátory, 7 izieb) a aplikácia
  sa správa presne ako verzia 1.x.
- Súbor nie je v gite (je len na serveri). Vzor: [`profil.example.json`](../profil.example.json).
- Chýbajúce položky sa doplnia z predvoleného profilu. Neplatná hodnota sa pri uložení odmietne s chybou po slovensky.
- **Po zmene profilu treba reštartovať služby** (`systemctl restart kurenie-agent kurenie-collector kurenie-web`, alebo
  tlačidlo „Reštartovať služby“ v sprievodcovi) – moduly si mapy načítajú pri štarte.

## Položky profilu

### `verzia`
Číslo verzie formátu (teraz `1`). Nemeň.

### `zariadenie`
| Kľúč | Význam |
|---|---|
| `model` | ID modelu = názov súboru v `modely/` bez `.json` (napr. `AE120JXEDGH`). Prázdne/neznáme = bez tabuľky výrobcu (pozri nižšie). Povolené znaky: písmená, číslice, `- _ .` |
| `nazov` | Zobrazovaný názov (do 80 znakov), ide aj do promptu agenta |
| `typ` | `split`, `mono` alebo `hydrosplit` |
| `vykon_kw` | Menovitý výkon, 2–30 kW |
| `fazy` | `1` alebo `3` |
| `chladivo` | Text (R410A, R32, R290 …), len informatívne |
| `kurenie` | `radiatory`, `podlahove` alebo `kombinovane` – určuje **predvolené mantinely agenta** |
| `tuv` | `true`/`false` – či je pripojený zásobník teplej vody. Pri `false` sa TÚV logika vypne |

### `esp`
Názvy entít tak, ako ich ESPHome zverejňuje cez `web_server` (aplikácia ich hľadá podľa presného názvu).
**Prázdny reťazec = entita nie je k dispozícii** (funkcia, ktorá ju potrebuje, sa potichu vypne). Vzorový ESPHome YAML
s týmito názvami: [`esphome/samsung-ehs.yaml`](../esphome/samsung-ehs.yaml).

`esp.entity` – kanonické kľúče:

| Kľúč | Význam | Povinné |
|---|---|---|
| `vonku` | vonkajšia teplota | áno |
| `tw2` | výstupná teplota vody | áno |
| `tw1` | spiatočka | áno |
| `freq` | frekvencia kompresora | áno |
| `wl` | cieľová ekvitermická voda (WL) | áno |
| `posun` | posun ekvitermickej krivky | áno |
| `sledovana` | izbová teplota, ktorú sleduje firmvér (referenčná izba) | odporúčané |
| `ciel_akt` | aktuálna cieľová teplota izby | nie |
| `ciel_izby` | nastaviteľná cieľová izbová teplota | nie |
| `dt` | Delta T (Tw2 − Tw1) | nie |
| `prikon` | príkon TČ (W) | nie |
| `tuv`, `tuv_ciel` | teplota a cieľ TÚV | len s TÚV |
| `pwm` | otáčky obehového čerpadla | nie |
| `chyba` | chybový kód | nie |
| `kwh_den` | denná spotreba TČ (kWh) | nie |
| `flow` | prietok vody (l/min) | nie |
| `ventil` | 3‑cestný ventil (smer ohrevu) | nie |
| `odmrazovanie` | odmrazovanie | nie |
| `termostat` | požiadavka na teplo | nie |

`esp.nastavenia` – nastavenia, ktoré smie agent meniť alebo ktoré sa sledujú: `fsv2011`, `fsv2012`, `fsv2021`, `fsv2022`,
`fsv2031`, `fsv2032`, `fsv4013`, `max_posun`, `max_posun_zaporny`, `zosilnenie`. (Povinné pre agenta: `fsv2031`, `fsv2032`.)

`esp.rest_extra` – zoznam ďalších entít, ktoré zberač číta cez REST (napr. hlavice radiátorov).

### `izby`
Zoznam izieb (aspoň jedna). Každá:

| Kľúč | Význam |
|---|---|
| `kluc` | krátky identifikátor: malé písmená bez diakritiky `a-z0-9_`, začína písmenom, max 21 znakov, **jedinečný**, nie rezervovaný (`ts`, `vonku`, `tw2`, `cop` … – zoznam `RESERVED` v `profil.py`). Je to názov stĺpca v `stats.db` – po prvom nasadení ho **nemeň** |
| `nazov` | zobrazovaný názov |
| `esp` | názov entity izbovej teploty v ESP (prázdne = len HA) |
| `ha` | zoznam entít `sensor.*` v Home Assistante (história, import) – až 5, poradie = priorita |
| `min` | cieľové minimum izby (°C, 16–26) alebo `null` (predvolene 21) |
| `referencna` | **práve jedna** izba má `true` |

Stĺpce nových izieb sa v `stats.db` dopĺňajú automaticky (`ALTER TABLE … ADD COLUMN`); nikdy sa nemažú.

### Referenčná izba
Referenčná je izba, ktorej teplotu sleduje samotné tepelné čerpadlo (v ESP entita `sledovana`) a podľa ktorej firmvér
posúva ekvitermickú krivku. Aplikácia ju používa ako **porovnávací základ**: rozdiely teplôt ostatných izieb sa počítajú
voči nej, „hodiny pod cieľom“ sa rátajú podľa nej a v promptoch agenta sa volá jej názvom. Referenčná izba nemá vlastné
cieľové minimum (`min` sa ignoruje – má ju cieľová teplota `ciel_izby`). Ak je v profile viac/žiadna referenčná izba,
validácia vráti chybu (pri načítaní sa použije prvá).

### `ine_teploty`
Ďalšie teploty len na sledovanie (nie izby pre reguláciu): `{"chodba": ["sensor.…"], …}`. Pre kľúč platia rovnaké pravidlá ako
pri izbách a nesmie sa zhodovať s izbou.

### `ha`
Zdroje histórie z Home Assistantu pre import (`stats_backfill`): `vonku`, `tw2`, `tw1`, `wl`, `tuv` → zoznam `entity_id`
(poradie = priorita, prázdny zoznam = bez importu).

### `mantinely`
Bezpečnostné hranice agenta. Kľúče:

- `ciel_izby`: `[min, max]` cieľovej izbovej teploty (absolútne hranice 16–26 °C).
- `fsv2031`, `fsv2032`, `fsv4013`, `max_posun`: `[min, max, max_krok, hodín_medzi_zmenami, zaokrúhlenie]`.

Ak kľúč chýba, použije sa **predvolená hodnota podľa typu kúrenia** (`zariadenie.kurenie`):

| | radiátory | podlahové | kombinované |
|---|---|---|---|
| FSV 2031 (voda pri najchladnejšom) | 42–50 °C | 30–40 °C | 35–45 °C |
| FSV 2032 (voda pri najteplejšom) | 32–38 °C | 24–32 °C | 28–35 °C |
| FSV 4013 (vypnutie nad vonk. teplotou) | 17–24 °C | 15–22 °C | 16–23 °C |
| max. kladný posun krivky | 1–4 | 0,5–2 | 0,5–3 |

Maximálny krok je 1 °C (posun 0,5) a minimálny odstup zmien 24 h (posun 12 h).

**Absolútne hranice** sú pevné a profil ich môže len **zúžiť**, nikdy rozšíriť (ochrana zariadenia a domu; hodnota mimo
hraníc sa pri uložení odmietne):

| Parameter | absolútne min–max |
|---|---|
| FSV 2031 | 25–55 °C |
| FSV 2032 | 20–45 °C |
| FSV 4013 | 10–35 °C |
| max. kladný posun | 0–5 |
| cieľová izbová teplota | 16–26 °C |

### `elektrina`
`ims_stat` (ID externej štatistiky v HA) a `meter_ids` (až 4 entity elektromera). Prázdne = použijú sa premenné
prostredia, resp. funkcia je vypnutá.

## Model a COP bez tabuľky

Súbory `modely/<id>.json` obsahujú tabuľku výrobcu *Maximum Heating Capacity (Peak)*: pre vonkajšiu teplotu (`ta`) a teplotu
výstupnej vody (`lw`) dvojicu `[HC kW, PI kW]` (`null` = mimo rozsahu tabuľky). Aplikácia z nej kalibruje prietok
a počíta COP. Pole `overene` hovorí, či bola tabuľka overená na reálnom zariadení (z knižnice je tak označený len
AE120JXEDGH; ostatné sú prepísané z dokumentácie výrobcu – pozri pole `zdroj` v každom súbore).

**Ak pre tvoj model tabuľka nie je** (model prázdny alebo súbor neexistuje):
- tepelný výkon a COP sa počítajú len ak je k dispozícii prietok zo senzora (`esp.entity.flow`) alebo z konfigurácie
  (`STATS_FLOW_LPM`, `STATS_FLOW_TUV_LPM` v `config.env`);
- ak nie je nič z toho, tepelný výkon a COP sú „nedostupné“ (žiadna chyba, ostatné funkcie fungujú);
- vlastný model pridáš skopírovaním niektorého súboru z `modely/`, úpravou čísel a `id` (názov súboru = `id`,
  `overene: false`) – použi **len čísla z dokumentácie výrobcu** (Technical Data Book) a uveď zdroj.

## Príklad: podlahovka, 3 izby, bez TÚV

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

Čo sa stane:
- `kurenie: podlahove` → mantinely FSV 2031 30–40 °C, FSV 2032 24–32 °C, posun 0,5–2 (agent dostane v prompte informáciu
  o podlahovke a nebude navrhovať radiátorové teploty vody).
- `tuv: false` a prázdne `tuv`/`tuv_ciel` → záložka TÚV sa skryje, plánovač TÚV nebeží.
- Referenčná je obývačka: podľa nej sa počítajú rozdiely a hodiny pod cieľom.
- `model: ""` → žiadna tabuľka; COP len ak je prietok zo senzora alebo z konfigurácie.
- Chýbajúce kľúče (`esp.nastavenia`, `elektrina`…) sa doplnia z predvoleného profilu – názvy entít v ESP musia preto
  sedieť s predvolenými (pozri `esphome/samsung-ehs.yaml`), alebo ich v profile prepíš.

Kompletnejší príklad (Mono 8 kW, podlahovka, 4 izby, bez TÚV): [`profil.example.json`](../profil.example.json).

## Sprievodca vo webe

Nastavenia → **Zariadenie a dom** (len správca). Jedna stránka v 4 sekciách:

1. **Zariadenie** – výber modelu z knižnice (alebo „iný/neznámy“), typ jednotky, výkon, fázy, typ kúrenia (po zmene sa
   ukážu predvolené mantinely) a či je TÚV.
2. **Entity ESP** – pre každý kanonický kľúč rozbaľovací zoznam entít nájdených v ESP (`ESP_URL`), predvyplnený
   automatickým návrhom podľa názvu, vedľa živá hodnota. Povinné sú vyznačené.
3. **Izby** – pridanie/odobratie/poradie, kľúč (generuje sa z názvu, upraviteľný len pri novej izbe), názov, ESP entita,
   HA senzory (výber z teplotných senzorov v HA), minimum a prepínač „referenčná“ (práve jedna).
4. **Mantinely** – min/max pre FSV 2031/2032/4013, maximálny posun a rozsah cieľovej izbovej teploty s absolútnymi hranicami.

Tlačidlá: **Uložiť** (chyby sa zobrazia pri poliach), **Obnoviť predvolené** (zmaže `profil.json` → pôvodná inštalácia)
a po uložení **Reštartovať služby**. Bez reštartu sa zmena neprejaví.
