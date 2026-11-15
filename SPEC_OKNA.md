# Okná a dvere, skupiny, sezóna leto/zima (špecifikácia)

Cieľ: agent kúrenia vie, kedy je v ktorej miestnosti otvorené okno/dvere, berie to do úvahy pri hodnotení a rozhodovaní,
a v zime pošle upozornenie, keď je okno otvorené dlhšie ako nastavený čas. Používateľ si na webe spravuje senzory
(zapnúť/vypnúť, premenovať pre agenta, zaradiť do skupiny), skupiny (vytvoriť, premenovať, zmazať, priradiť izbu) a sezónu.
Python iba štandardná knižnica, texty po slovensky. Zachovať všetko existujúce (najmä porovnávací test A/B, auth, rutinu).

## Zdroj dát: Home Assistant (REST API, dlhodobý token)
- Config: `HA_URL` (predvolene `http://homeassistant.local:8123` – z LXC sa prekladá na LAN adresu HA), `HA_TOKEN` (dlhodobý prístupový
  token, zadá ho používateľ na webe v Nastaveniach; ukladá sa do `config.env` ako OpenRouter kľúč, späť sa nezobrazuje, len posledné
  4 znaky). Token sa nikdy nevypisuje do logu ani do vstupu modelu.
- Čítanie jedným malým dopytom: `POST {HA_URL}/api/template` s hlavičkou `Authorization: Bearer <token>` a telom
  `{"template": "<jinja>"}`, ktorá vráti JSON zoznam všetkých `binary_sensor` s `device_class` v `door|window|opening|garage_door`:
  `[{"id": entity_id, "name": friendly name, "dc": device_class, "state": "on|off|unavailable|unknown",
  "since": last_changed ISO, "area": area_name(entity_id) alebo null}]`. (Šablóna: `{{ states.binary_sensor | selectattr(...)... | list | tojson }}`
  alebo cyklus s `namespace`; overiť, že HA vráti platný JSON.) Timeout 10 s.
- Slučka `contacts_loop` ako ďalšie daemon vlákno v procese agenta (ako strážca), každých 20 s; všetko v try/except.
  Bez tokenu nerobí nič. Chyba spojenia: po 3 neúspechoch po sebe poplach `ha` v `state.alerts` („Home Assistant neodpovedá…“),
  zmizne po úspechu; upozornenie na telefón len raz za 6 h.
- `on` = otvorené, `off` = zatvorené, ostatné = nedostupné.

## Databáza (`agent.db`, migrácie cez `CREATE TABLE IF NOT EXISTS`)
- `contact_groups(id INTEGER PRIMARY KEY, name TEXT, room TEXT NULL, notify_min INTEGER NULL, sort INTEGER)`
  `room` = kľúč izby (`detska`, `jedalen`, `kuchyna`, `obyvacka`, `spalna`, `pracovna`, `kupelna`) alebo NULL (napr. chodba).
  `notify_min` NULL = použije sa globálne nastavenie.
  Predvolené skupiny pri prvom spustení (ak je tabuľka prázdna): Obývačka(obyvacka), Detská(detska), Jedáleň(jedalen),
  Kuchyňa(kuchyna), Spálňa(spalna), Pracovňa(pracovna), Kúpeľňa(kupelna), Chodba(NULL), Ostatné(NULL).
- `contacts(entity_id TEXT PRIMARY KEY, ha_name TEXT, alias TEXT NULL, group_id INTEGER NULL, enabled INTEGER, device_class TEXT,
  state TEXT, since INTEGER, area TEXT, note TEXT NULL, first_seen INTEGER, last_seen INTEGER)`
  - Nový senzor (prvé objavenie): skupina podľa `area` alebo mena (obsahuje obyvack/detsk/jedal/kuchyn/spaln/pracovn/kupel/chodb
    bez diakritiky, malé písmená) → zodpovedajúca skupina, inak „Ostatné“.
  - `enabled` predvolene 1 len ak je zaradený do inej skupiny než „Ostatné“ a entity_id ani meno neobsahuje `skoda`, `octavia`,
    `umyvacka`, `rura`, `radiator` (hlavica len odhaduje okno). Inak 0.
  - Výnimka pri prvom naplnení: `binary_sensor.kuchyna_dvere_kontakt_sensor_contact` → enabled 0, note „pokazený – odpadnutý magnet
    (10. 11. 2026)“. `binary_sensor.0x00158d000272a9d3_contact` (Chodba_dvere_kontakt_sensor) → skupina Chodba, enabled 1.
  - Senzor, ktorý z HA zmizne, ostane v tabuľke so stavom „nedostupné“ (nemazať, používateľ ho môže vypnúť).
- `contact_events(id INTEGER PRIMARY KEY, ts INTEGER, entity_id TEXT, state TEXT)` – zapisuje sa každá zmena stavu (aj pre vypnuté
  senzory, aby sa dala história ukázať), čas = `last_changed` z HA. Index na ts.
- `state` kľúče: `season` (`zima`|`leto`, predvolene `zima`), `window_notify_min` (predvolene 10, rozsah 1–240),
  `window_repeat_min` (pripomienka, predvolene 30, 0 = vypnuté, rozsah 0–240).

## Pomocné funkcie (`contacts.py`, nový modul)
- `poll(cfg, c)`, `contacts_loop()`, `list_contacts(c)`, `groups(c)`, CRUD skupín, `update_contact(c, entity_id, alias, group_id, enabled, note)`.
- `open_now(c, now)` → zoznam zapnutých (`enabled=1`) senzorov, ktoré sú otvorené: `{entity_id, label (alias alebo ha_name), group, room, minutes}`.
- `open_minutes(c, a, b, only_enabled=True)` → `{group_id: minúty otvorenia v [a,b)}` a `{room: minúty}` z `contact_events`
  (rekonštrukcia intervalov; začiatok intervalu pred `a` dopočítaj z posledného stavu pred `a`; prekrývajúce sa okná v jednej
  skupine počítaj ako zjednotenie času, nie súčet).
- `hourly_open(c, a, b)` → pre každú hodinu `{room_or_group: minúty}` (pre hodinovú tabuľku webu a vstup modelu).
- `label(c, entity_id)` = alias, inak ha_name. Agent a web používajú vždy `label`.

## Upozornenia (zima)
- Len ak `season == "zima"`. Pre každý zapnutý otvorený senzor: keď je otvorený ≥ `notify_min` (skupiny, inak globálne) minút →
  upozornenie `okno` „Otvorené okno: <skupina> – <label> už <N> min. Vonku je <t> °C.“ (vonkajšiu teplotu z tc.db).
  Ďalšie pripomienky každých `window_repeat_min` minút, kým je otvorené (0 = žiadne). Po zatvorení, ak už odišlo upozornenie,
  pošli krátke „Zatvorené: <label> po <N> min.“. Pamäť o odoslaných upozorneniach na otvorenie drž v `state` (napr. JSON podľa
  entity_id + since), aby reštart neposlal duplikát. Cooldown v notify.py pre `okno` = 0 (riadi to táto logika).
- V lete sa upozornenia na okná neposielajú vôbec.

## Agent (zohľadnenie okien a sezóny)
- `build_input`: sekcia „OKNÁ A DVERE (sezóna: zima|leto)“: aktuálne otvorené (skupina, label, minúty), za posledných 24 h minúty
  otvorenia po skupinách a počet otvorení; v hodinovej tabuľke 24 h nový stĺpec „okná min“ (súčet minút otvorenia v danej hodine,
  zjednotenie cez celý dom). Vypnuté senzory sa nepoužívajú.
- `SYSTEM` prompt doplniť: v zime otvorené okno vysvetľuje pokles teploty v danej izbe – nepovažuj ho za dôvod zvyšovať krivku
  a nenavrhuj zmenu kvôli izbe, kde bolo za posledné 2 h okno otvorené ≥ 10 min; dlhé vetranie v zime (> 15 min) môžeš spomenúť
  v odporúčaní. V lete je vetranie v poriadku, kúrenie zvyčajne nebeží, nehodnoť izby voči minimám a nenavrhuj zvyšovanie krivky.
- `check_action`: v zime odmietni zvýšenie FSV 2031/2032 alebo max. kladného posunu, ak bol za posledné 2 h v dome otvorený
  zapnutý senzor ≥ 15 min (poznámka „otvorené okno – pokles teploty má jasnú príčinu“). V lete odmietni akékoľvek zvýšenie krivky
  („letná sezóna“).
- `needs_llm`: podmienky „detská mimo cieľa“ a „izba pod minimom“ ignoruj pre izby, kde bolo za poslednú hodinu otvorené okno
  ≥ 5 min; v lete ich ignoruj úplne. Nová podmienka (len zima): „okno otvorené ≥ 30 min“ → zavolať model (raz na otvorenie).
- `event_trigger`: izbové podmienky preskoč pre izbu s oknom otvoreným v posledných 30 min; v lete izbové podmienky vôbec.
- `interventions.evaluate_due`: do `before`/`after` pridaj `okna_min` (minúty otvorenia, celý dom) a ak sa líšia o > 60 min,
  doplň do verdiktu vetu „Počas jedného z období sa viac vetralo, porovnanie je menej spoľahlivé.“
- Porovnávací test A/B (`abtest.evaluate`): pre každý deň pridaj `okna_min`; v súhrne priemer pre A a B. Deň s > 120 min otvorených okien
  označ v poznámke („veľa vetrania“), nevyraďuj ho. Poistky testu sa nemenia.
- Hodinová štatistika pre web `api/day` dostane `okna_min` na hodinu a `okna` = zoznam udalostí otvorenia/zatvorenia dňa
  (`ts, label, group, state`).
- Učenie (`learn`) a týždenný súhrn dostanú v denných súhrnoch minúty otvorenia okien a aktuálnu sezónu.

## API (prihlásenie a `X-Kurenie: 1` ako ostatné)
| Metóda a cesta | Telo | Odpoveď |
|---|---|---|
| GET `/api/contacts` | – | `{"season","notify_min","repeat_min","ha":{"configured","ok","last_ok","error","token_hint"}, "groups":[{"id","name","room","notify_min","sort","sensors":[{"entity_id","ha_name","alias","label","enabled","device_class","state","since","area","note","open_min"}]}], "ungrouped":[...]}` |
| POST `/api/contacts/settings` | `{"season"?, "notify_min"?, "repeat_min"?}` | `{"ok":true}` / 400 |
| POST `/api/contacts/sensor` | `{"entity_id", "alias"?, "group_id"?, "enabled"?, "note"?}` (alias "" = zrušiť, max 60 znakov) | `{"ok":true}` / 400 |
| POST `/api/contacts/group` | `{"id"?, "name", "room"?, "notify_min"?}` (bez id = nová, s id = úprava) | `{"ok":true,"id"}` / 400 |
| POST `/api/contacts/group_delete` | `{"id"}` – senzory sa presunú do „Ostatné“; „Ostatné“ sa zmazať nedá | `{"ok":true}` / 400 |
| POST `/api/settings` | navyše `ha_url`, `ha_token` (rovnako ako api_key) | – |
| POST `/api/ha_test` | `{}` | `{"ok", "count", "error"}` – počet nájdených senzorov |
`/api/status` dostane `season` a `windows_open: [{"label","group","minutes"}]` (len zapnuté).

## Web (`ui.html`, zachovať vizuálny jazyk, svetlý aj tmavý režim, mobil 380 px)
- Horná lišta: prepínač sezóny **Zima / Leto** (segmentový ovládač, uloží hneď, krátke vysvetlenie v title). Ak je niečo otvorené,
  v hlavičke pilulka „Otvorené: 2 okná“ (odkaz na panel).
- Hero/Teraz: pri izbe na teplotnej stupnici malá ikonka/označenie otvoreného okna.
- Nový panel **„Okná a dvere“** (pred Nastaveniami):
  - hore: sezóna slovne a jednou vetou čo znamená; „Upozorniť po N min“ (číselné pole), „Pripomínať každých N min (0 = nie)“; stav
    spojenia s Home Assistantom (alebo výzva vložiť token v Nastaveniach).
  - skupiny ako karty; v karte zoznam senzorov: stav (Otvorené N min / Zatvorené / Nedostupné), meno pre agenta (input s pôvodným
    menom z HA ako placeholder a malým textom pod ním), výber skupiny, prepínač Zapnutý (vypnutý senzor je zošednutý s textom
    „agent ho ignoruje“), poznámka. Ukladanie po zmene poľa (blur/change), krátke potvrdenie „Uložené“.
  - úprava skupiny: názov, prepojená izba (výber z izieb alebo „žiadna“), vlastný čas upozornenia (prázdne = podľa celku), zmazať
    s potvrdením v stránke; tlačidlo „Pridať skupinu“.
  - vypnuté senzory a „Ostatné“ (auto, spotrebiče) zbalené pod „Ostatné senzory (N)“, aby neprekážali.
- Nastavenia → nová skupina „Home Assistant“: adresa, token (ako pole kľúča: password + Zobraziť, späť sa nezobrazuje, posledné
  4 znaky), „Otestovať spojenie“ (ukáže počet nájdených senzorov), krátky návod: token vytvoríš v Home Assistante v profile →
  Zabezpečenie → Dlhodobé prístupové tokeny.
- Deň: v tabuľke hodín stĺpec „Okná“ (minúty, zvýraznené ≥ 15 v zime); v detaile hodiny zoznam otvorení/zatvorení; v grafe izieb
  jemné zvislé pruhy v hodinách s otvoreným oknom (≥ 5 min).
