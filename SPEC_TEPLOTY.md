# Teplotné čidlá z Home Assistantu (špecifikácia)

Cieľ: rovnaké riešenie ako pri kontaktných senzoroch (`contacts.py`, panel „Okná a dvere“, viď `SPEC_OKNA.md`), ale pre teplotné
čidlá. Používateľ si na webe vyberie, ktoré čidlá agent berie do úvahy, premenuje ich len pre agenta, zaradí do TÝCH ISTÝCH skupín
(`contact_groups`) a povie, čo čidlo meria. Agent ich dostane vo vstupe. Iba štandardná knižnica, texty po slovensky, nič
existujúce sa nesmie pokaziť. Žiadne nové zápisy do ESP ani do Home Assistantu – len čítanie.

## Zdroj dát
- Rovnaké pripojenie ako kontakty (`HA_URL`, `HA_TOKEN`, `POST /api/template`). V `contacts_loop` pribudne každých 60 s druhý dopyt
  (alebo samostatná slučka `temps_loop` v tom istom duchu). Chyby spojenia používajú existujúci poplach `ha` – nevytvárať druhý.
- Šablóna (overená v ostrom HA, vracia 56 čidiel):
```
{%- set ns = namespace(r=[]) -%}
{%- for s in states.sensor if s.attributes.device_class == 'temperature' -%}
{%- set ns.r = ns.r + [{'id': s.entity_id, 'name': s.name, 'state': s.state, 'unit': s.attributes.unit_of_measurement, 'since': s.last_updated.isoformat(), 'area': area_name(s.entity_id)}] -%}
{%- endfor -%}
{{ ns.r | tojson }}
```
  `state` je text: číslo alebo `unavailable`/`unknown`. Jednotka `°C` (ak `°F`, prepočítaj). Hodnoty mimo −60…150 ber ako neplatné.

## Databáza (`agent.db`)
- `temp_sensors(entity_id TEXT PRIMARY KEY, ha_name TEXT, alias TEXT NULL, group_id INTEGER NULL, enabled INTEGER, kind TEXT,
  value REAL NULL, updated INTEGER, area TEXT, note TEXT NULL, alert_min REAL NULL, alert_max REAL NULL, first_seen INTEGER, last_seen INTEGER)`
  - `kind` (čo čidlo meria): `izba` (vzduch v miestnosti), `radiator` (hlavica pri radiátore – ukazuje viac než izba), `vonku`,
    `voda` (vykurovacia voda, bojler, čerpadlo), `technicke` (čipy, elektronika, spotrebiče), `ine`.
  - Predvolené zaradenie pri prvom objavení: skupina podľa `area`/mena rovnakou logikou ako kontakty (bez diakritiky; `kotoln`,
    `technick` → skupina „Ostatné“).
  - Predvolený `kind` podľa entity_id/mena (bez diakritiky, malé): obsahuje `radiator` → `radiator`; `vonkaj`, `outside`, `irimav`,
    `rosny`, `pocitov`, `index_tepla` → `vonku`; `m5stack`, `tuv`, `bojler`, `tw1`, `tw2`, `voda`, `vymennik`, `vytlak`, `delta` → `voda`;
    `slzb`, `chip`, `cip`, `switch`, `skoda`, `octavia`, `rura`, `oven`, `probe`, `cpu`, `nspanel`, `kontakt`, `okno_kontakt`,
    `dvere_kontakt` → `technicke`; inak ak je v izbovej skupine → `izba`, inak `ine`.
  - Predvolene `enabled=1` len pre `kind == izba` v izbovej skupine a so stavom, ktorý je číslo; všetko ostatné 0.
    (Čidlá m5stack-tc sú už v zberači – nechaj ich vypnuté, aby sa dáta nezdvojovali.)
- `temp_samples(ts INTEGER, entity_id TEXT, value REAL)` + index (entity_id, ts): vzorka každých 5 min pre ZAPNUTÉ čidlá s platnou
  hodnotou. Raz denne zmaž vzorky staršie ako 90 dní.
- `state`: `temp_notify_min` (predvolene 15, rozsah 1–240) – po koľkých minútach mimo hranice poslať upozornenie.

## Upozornenia
- Len pre zapnuté čidlá s vyplneným `alert_min` a/alebo `alert_max`: keď je hodnota nepretržite pod `alert_min` / nad `alert_max`
  aspoň `temp_notify_min` minút → upozornenie `teplota` („Nízka teplota: <skupina> – <label> má <x> °C (hranica <y> °C) už <N> min.“),
  opakovanie najviac raz za 3 h, a jedno „späť v norme“ po návrate (len ak predtým odišlo upozornenie). Pamäť v `state`, aby reštart
  neposlal duplikát. Nezávisí od sezóny. Čidlo nedostupné > 60 min: žiadne upozornenie (len stav na webe).

## Agent
- `build_input`: sekcia „TEPLOTNÉ ČIDLÁ Z HOME ASSISTANTU (vybral majiteľ)“ – po skupinách: label, druh, aktuálna hodnota, priemer/min/max
  za 24 h, zmena za posledné 3 h; nedostupné označ. Vysvetli druhy jednou vetou (hlavica radiátora nie je teplota izby).
  Ak je sekcia prázdna, vynechaj ju. Vypnuté čidlá sa nepoužívajú nikde.
- `SYSTEM` prompt: tieto čidlá sú doplnok k hlavným izbovým teplotám zo zberača; ak majiteľ zapol čidlo v miestnosti, ktorú zberač
  nemá (napr. kotolňa, chodba), môže ho agent použiť pri hodnotení; pri rozpore medzi čidlami v jednej izbe na to upozorni.
- Hodnotenie zásahov, porovnávací test A/B, „rutina“, `needs_llm`, `event_trigger`, mantinely – BEZ ZMENY (naďalej dáta zo zberača).
- Učenie a týždenný súhrn: pridaj denný priemer/min/max zapnutých čidiel (max 12 čidiel) do vstupu.

## API (prihlásenie + `X-Kurenie: 1`)
| Metóda a cesta | Telo | Odpoveď |
|---|---|---|
| GET `/api/temps` | – | `{"notify_min", "ha": {...ako pri contacts}, "kinds": [{"key","name"}], "groups":[{"id","name","room","sensors":[{"entity_id","ha_name","alias","label","enabled","kind","value","updated","area","note","alert_min","alert_max","avg24","min24","max24","available"}]}], "other_group_id"}` (skupiny tie isté ako pri kontaktoch) |
| POST `/api/temps/sensor` | `{"entity_id", "alias"?, "group_id"?, "enabled"?, "kind"?, "note"?, "alert_min"?, "alert_max"?}` (alias "" = zrušiť, max 60 znakov; alert null = bez hranice; rozsah −40…120, min < max) | `{"ok":true}` / 400 |
| POST `/api/temps/settings` | `{"notify_min"}` | `{"ok":true}` / 400 |
`/api/day` dostane `temps: [{"label","group","kind","avg","min","max"}]` pre zapnuté čidlá (z `temp_samples` daného dňa).
Zmazanie skupiny (`/api/contacts/group_delete`) musí presunúť do „Ostatné“ aj teplotné čidlá.

## Web (`ui.html`)
- Nový panel **„Teplotné čidlá“** hneď za panelom „Okná a dvere“, rovnaký vizuál a ovládanie: stav spojenia s HA, skupiny ako
  karty, v karte čidlá: aktuálna hodnota (veľká, s časom poslednej aktualizácie; „Nedostupné“), meno pre agenta (placeholder = meno
  z HA), výber skupiny, výber druhu, prepínač Zapnuté (vypnuté zošednuté „agent ho ignoruje“), hranice upozornenia „pod °C“ / „nad °C“
  (prázdne = bez upozornenia), poznámka; pri zapnutých malý text „24 h: Ø x · min y · max z“. Ukladanie po zmene poľa, potvrdenie
  „Uložené“. Hore: „Upozorniť po N minútach mimo hranice“.
- Vypnuté čidlá zbalené v karte skupiny pod „Vypnuté čidlá (N)“, skupina „Ostatné“ celá zbalená – čidiel je 56, zoznam nesmie byť dlhý.
- Vyhľadávacie pole nad skupinami (filtruje podľa mena, aliasu, entity_id), aby sa dalo čidlo rýchlo nájsť a zapnúť.
- Úprava skupín ostáva v paneli „Okná a dvere“ (tu len odkaz „Skupiny upravíš v paneli Okná a dvere“).
- Deň: pod tabuľkou hodín malá tabuľka „Čidlá z Home Assistantu“ (label, skupina, druh, Ø / min / max dňa), len ak nejaké sú.
- Navigácia: odkaz „Čidlá“.
