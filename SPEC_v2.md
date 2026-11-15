# SPEC v2.0 – nastaviteľné pre iné tepelné čerpadlá Samsung EHS (15. 11. 2026)

Cieľ: aplikáciu môže nasadiť ktokoľvek s iným Samsung EHS (Mono/Split, iný výkon, iné izby, iný ESPHome YAML, podlahovka),
bez úprav kódu – všetko inštalačne špecifické je v **profile** (`profil.json`, spravuje modul `profil.py`).
**Bez `profil.json` sa aplikácia musí správať presne ako verzia 1.0.0** (predvolený profil = pôvodný dom).

## Spoločné pravidlá pre všetkých
- Pracovný priečinok: `gh_kurenie/` (kópia repozitára). Python: `"C:\Program Files\KiCad\10.0\bin\python.exe" -X utf8`.
  Nič nenasadzuj, žiadne ssh/scp/git push. Len štandardná knižnica. Texty po slovensky.
- **`profil.py` je hotový a je to zmluva** – nemeň ho (ak niečo chýba, napíš to do odovzdania). Prečítaj si jeho docstring a API.
  `test_profil_v1.json` = presné hodnoty konštánt z v1 (S, TC, SETTINGS, ROOMS, ROOM_NAMES, TARGET_ROOMS, TARGET_DEFAULTS,
  ALLOWED, ROOM_TARGET, COLUMN_IDS, VALVE, DEFROST, THERMOSTAT, AGENT_TEMPS, ROOM_TARGET_ENT).
- Vzor refaktoru: **konštanty ostávajú pod pôvodnými menami**, len sa naplnia z profilu pri importe, napr.
  `P = profil.load(); S = profil.esp_s(P); INV = {v: k for k, v in S.items()}`. Tak ostatný kód netreba meniť.
  Kde je v texte/promptoch natvrdo „detská“ ako referenčná izba, použi `profil.room_names(P)[profil.reference_room(P)]`.
- Entita, ktorá v profile chýba (prázdny reťazec / kľúč chýba v mape), nesmie spôsobiť pád – funkcia, ktorá ju potrebuje, sa
  potichu vypne alebo vráti None (napr. bez TÚV entít žiadna TÚV logika, bez prietoku prietok z tabuľky/konfigurácie).
- Upravuj **len súbory pridelené tvojej úlohe** (iné úlohy bežia súčasne). Pôvodné testy musia prejsť bez zmeny očakávaných hodnôt
  (môžeš upraviť len import/setup, ak je to nevyhnutné – zdôvodni). Pridaj test, že s predvoleným profilom sú konštanty modulu
  totožné s `test_profil_v1.json`, a test s iným profilom (napr. 3 izby, podlahovka, bez TÚV).

## Úloha A1 – jadro agenta a zberač
Súbory: `common.py`, `agent.py`, `collector.py`, `abtest.py`, `interventions.py`, `contacts.py`, `temps.py`, `dhw.py`,
`notify.py`, `ha_ws.py`, `ha_pub.py`, `ha_cost.py`, `cfjwt.py`, `cftunnel.py`, `webpush.py`, `contacts_mqtt.py`; nový `test_v2_jadro.py`.
- `common.py`: `S`, `INV`, `SETTINGS`, `ROOMS`, `ROOM_NAMES`, `TARGET_ROOMS`, `TARGET_DEFAULTS` z profilu; `day_summary` (rozdiel_* voči
  referenčnej izbe, `hod_pod_cielom` podľa referenčnej izby – teraz natvrdo "detska"); ESP_URL ostáva v config.env.
- `agent.py`: `ALLOWED = profil.limits(P)`, `CURVE_ENTITIES`/`RAISE_CURVE` z `profil.setting_entity`, `ROOM_TARGET_ENT`,
  `ROOM_TARGET_MIN/MAX = profil.room_target_range(P)`; v promptoch modelu nahraď natvrdo izby/„detská“/zoznam izieb
  (`"izba": "detska|jedalen|…"`) hodnotami z profilu; v systémovom prompte doplň 1 riadok o zariadení (`P["zariadenie"]["nazov"]`,
  typ kúrenia) – nech model vie, či ide o podlahovku. Mantinely sa tým automaticky prispôsobia typu kúrenia.
- `collector.py`: `REST_POLL` = ESP názvy izieb z profilu + `P["esp"]["rest_extra"]`; ESP adresa: `TC_ESP` env, inak `ESP_URL` z config.env
  (cez common.load_cfg), inak doterajšia predvolená.
- `ha_ws.py`: `ENERGY_EXT_ID` / `ENERGY_METER_IDS` – prednosť má `P["elektrina"]` (ak neprázdne), potom env, potom súčasné predvolené.
- `contacts.py` `KITCHEN_DOOR` a iné natvrdo zadané entity: ponechaj ako voliteľné (ak entita v HA neexistuje, nič sa nedeje) – len over.
- `abtest.py`, `interventions.py`: referenčná izba a názvy izieb z profilu.

## Úloha A2 – štatistiky, COP a model zariadenia
Súbory: `stats.py`, `stats_backfill.py`, `stats_ui.js`, `stats_ui.css`, `vonku.py`, `spotreba.py`, nový `modely/AE120JXEDGH.json`,
testy `test_cop.py`, `t_energia.py`, `test_vonku.py`, `test_spotreba.py`, nový `test_v2_stats.py`.
- `stats.py`: `TC`, `INV`, `VALVE`, `DEFROST`, `THERMOSTAT`, `AGENT_TEMPS` z profilu (`AGENT_TEMPS = {k: ids[0] …}` z `ine_teploty`).
- **Dynamické izby v `hourly`**: stĺpce izieb = `profil.room_keys(P)` + `profil.extra_temp_keys(P)`; pri `connect` doplň chýbajúce
  stĺpce `ALTER TABLE hourly ADD COLUMN <kluc> REAL` (kľúč je validovaný regexom v profile; stĺpce sa nikdy nemažú). `HCOLS`, `AVG_KEYS`,
  metriky (`_m(...)` zoznam pre UI štatistík) pre izby generuj z profilu. Pozor na poradie stĺpcov – nech je s predvoleným profilom
  rovnaké ako vo v1.
- **Model**: presuň `TAB_TA`, `TAB_LW`, `TAB_PEAK` do `modely/AE120JXEDGH.json` vo formáte, ktorý číta `profil._read_model`:
  `{"id","nazov","typ":"split","vykon_kw":12,"chladivo":"R410A","zdroj":"Samsung Technical Data Book AE120JXED*H/EU, Maximum Heating Capacity (Peak)","overene":true,"ta":[…],"lw":[…],"peak":{"-20":[[HC,PI]|null,…],…}}`.
  `stats.py` načíta model cez `profil.model(P)`; `cop_tab` a `flow_calc` pracujú s tabuľkou modelu. **Bez modelu** (None): `cop_tab`
  vráti None, kalibrácia prietoku z tabuľky sa preskočí a použije sa prietok zo senzora (`flow`) alebo z konfigurácie
  (`STATS_FLOW_LPM`, `STATS_FLOW_TUV_LPM`); ak nič, tepelný výkon/COP = None (nie pád). `FLOWCAL_ZDROJ` text podľa modelu.
- `stats_backfill.py`: `COLUMN_IDS = profil.ha_columns(P)` (VALUE_COLS odvodené).
- `vonku.py`: `IZBY` = izby z profilu + `ine_teploty`; názvy z profilu; entity krivky (FSV…) cez `profil.setting_entity`/`device_entity`.
- `spotreba.py`: bez zmeny logiky, len over, že nič natvrdo neviaže na izby.
- `stats_ui.js`: ak obsahuje natvrdo zoznam izieb/kľúčov, prečítaj ich z API (metriky už posiela stats.py).

## Úloha B – web: profil, sprievodca, vypínanie funkcií
Súbory: `app.py`, `ui.html`, `sw.js`; nový `test_v2_web.py`.
- API (len admin): `GET /api/profil` → `{profil, chyby, modely: profil.list_models(), typy_kurenia, typy_jednotky, abs_limity, predvolby: PRESETS}`;
  `POST /api/profil` {profil} → `profil.save`; chyby → 400 `{chyby:[…]}`; úspech → `{ok, restart_potrebny: true}`.
  `POST /api/profil/predvoleny` → zmaže `profil.json` (návrat na predvolený).
  `GET /api/profil/esp_entity` → zoznam entít z ESP (`<ESP_URL>` web_server: skús `GET /` HTML a vyparsuj názvy entít z tabuľky,
  a/alebo posledné hodnoty z tc.db `samples` za 10 min – `SELECT entity, value, max(ts) … GROUP BY entity`) ako
  `[{nazov, hodnota, zdroj}]` + `navrh` = automatické priradenie kanonických kľúčov (`profil.DEVICE_KEYS`, `SETTING_KEYS`) podľa zhody
  názvu (bez diakritiky, malé písmená; kľúčové slová napr. tw2/výstup, tw1/spiatočka, wl/ekviterm, frekvencia, odmrazovanie, ventil,
  2031, 2032, …). `GET /api/profil/ha_entity` → teplotné senzory z HA (ak je HA_TOKEN; `sensor.*` s device_class temperature: entity_id,
  meno, stav) pre výber izieb.
  `POST /api/restart` (admin, hlavička `X-Kurenie: 1`) → asynchrónne `systemctl restart kurenie-agent kurenie-collector`, potom seba
  (`kurenie-web`) s oneskorením 1 s; v DRY_RUN alebo bez systemctl len vráti `{ok:false, dovod}`.
- `/api/status`: pridaj `profil_info` = `{zariadenie: nazov, kurenie, izby: [{kluc, nazov, referencna}], funkcie: {...}}`, kde
  `funkcie` = `{ha: bool(HA_TOKEN), agent_ai: bool(OPENROUTER_API_KEY), tuv: zariadenie.tuv and entita tuv, spotreba_domu: existujú
  dáta kwh_dom alebo ims, model_cop: profil.model() is not None}` – aj pre rolu user (bez citlivých údajov).
- `ui.html`:
  - Všetky natvrdo zadané zoznamy izieb (napr. `ROOMS` v JS, „Detská“ ako referencia) nahraď údajmi z `profil_info` (fallback = súčasné).
  - Funkcie, ktoré nie sú k dispozícii, skry (záložka TÚV bez tuv, Spotreba bez spotreba_domu, karta Agent bez agent_ai ukáže
    „Agent je vypnutý – vlož kľúč OpenRouter“), namiesto chýb.
  - Nastavenia → nová karta **„Zariadenie a dom“** (len admin): sprievodca v 4 krokoch na jednej stránke (sekcie):
    1. Zariadenie: model (výber z `modely` + „iný/neznámy“), typ jednotky, výkon, fázy, typ kúrenia (radiátory/podlahové/kombinované –
       pri zmene ukáž predvolené mantinely), TÚV áno/nie.
    2. Entity ESP: tabuľka kanonický kľúč → rozbaľovací zoznam entít z ESP (predvyplnené `navrh`), vedľa živá hodnota; povinné
       vyznač (vonku, tw2, tw1, freq, wl, posun, fsv2031, fsv2032), ostatné voliteľné.
    3. Izby: zoznam (pridať/odobrať/poradie), kľúč (generuj z názvu bez diakritiky, editovateľný len pri novej izbe), názov, ESP entita,
       HA senzory (výber z `/api/profil/ha_entity`), minimum, prepínač referenčná (práve jedna).
    4. Mantinely: min/max pre fsv2031/fsv2032/fsv4013/max_posun a rozsah cieľovej izbovej teploty (s absolútnymi hranicami, predvolené
       podľa typu kúrenia).
    Tlačidlá „Uložiť“ (zobraz chyby z API pri poliach), „Obnoviť predvolené“, a po uložení „Reštartovať služby“ s vysvetlením.
    Mobil: bez vodorovného posúvania. Štýl ako ostatné karty Nastavení.
- `sw.js`: zvýš verziu cache, aby sa nový ui.html načítal.

## Úloha C – knižnica modelov, ESPHome šablóna, dokumentácia
Súbory (len nové/uvedené): `modely/*.json` **okrem** `modely/AE120JXEDGH.json`, `esphome/samsung-ehs.yaml`, `esphome/README.md`,
`docs/PROFIL.md`, `profil.example.json`, `README.md`, `CHANGELOG.md`.
- `modely/`: tabuľky výkonu/príkonu (Maximum/Peak heating capacity: HC kW, PI kW podľa vonkajšej teploty a teploty výstupnej vody)
  pre bežné Samsung EHS (napr. Mono R32 gen. 6/7: AE050/AE080/AE120/AE160 RXYD…, HT Quiet AE..BXYD…, Split AE..JXED/RXED…).
  **Používaj len čísla z dohľadateľného zdroja** (Samsung Technical Data Book / Service manual – uveď presný názov dokumentu a URL v poli
  `zdroj`) a nastav `"overene": false` (overené na reálnom zariadení je len AE120JXEDGH). Čo nevieš dohľadať, nevymýšľaj – radšej menej
  modelov. Formát musí prejsť `profil._read_model` (over skriptom).
- `esphome/samsung-ehs.yaml`: vzorová konfigurácia pre komponent `Beormund/esphome-samsung-nasa` s `web_server` a názvami entít
  presne podľa `profil.default()["esp"]` (aby nebolo treba nič mapovať); komentáre, `!secret` pre heslá, UART piny ako premenné.
  `esphome/README.md` – zapojenie (F1/F2 NASA zbernica, M5Stack Atom + RS485), nahratie.
- `docs/PROFIL.md`: popis každej položky profilu, príklad pre podlahovku s 3 izbami, ako sa rieši referenčná izba, mantinely a absolútne
  hranice, model/COP bez tabuľky, postup sprievodcu vo webe.
- `profil.example.json`: príklad (iný dom než predvolený: Mono 8 kW, podlahovka, 4 izby, bez TÚV entít). Musí prejsť `profil.validate`
  bez chýb (over skriptom).
- `README.md`: sekcia „Iné tepelné čerpadlo / vlastný dom“ (profil, sprievodca, modely, ESPHome šablóna), aktualizuj „Prispôsobenie“;
  `CHANGELOG.md`: sekcia `[2.0.0] – nevydané` so zmenami v2 (všetky úlohy A1, A2, B, C).

## Integrácia (robí hlavný agent)
Spustenie všetkých testov, porovnanie s v1 na kópii produkčných dát (náhľad len na čítanie), až potom VERSION 2.0.0, tag a nasadenie.
