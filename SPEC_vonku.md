# SPEC: „Teploty podľa vonku“ – min/max v izbách pri danej vonkajšej teplote (15. 11. 2026)

Cieľ: v záložke **Izby** tlačidlo **„Podľa vonkajšej teploty“**, ktoré zobrazí, aké teploty (najnižšia, priemer, najvyššia) boli
v jednotlivých izbách a vo vykurovacej vode pri danej vonkajšej teplote. Len štandardná knižnica Pythonu, texty po slovensky.
Nič nenasadzuj, na server nechoď.

## Dáta
`stats.db`, tabuľka `hourly` (stats.py, otvárať len na čítanie `stats._ro(stats._db_path(cfg), stats._timeout(cfg))`):
`ts` (začiatok hodiny), `vonku` (hodinový priemer), izby `detska, jedalen, kuchyna, obyvacka, spalna, pracovna, kupelna, chodba`
(hodinové priemery, história z HA od 10/2025; detská má zatiaľ len pár dní), voda `tw2` (výstup), `tw1` (spiatočka), `wl` (cieľ podľa
krivky) – tie až od 6. 11. 2026, `kwh_cal`/`kwh` (čerpadlo kWh za hodinu), `beh_min`. Chýbajúce stĺpce/DB → prázdny výsledok, nie chyba
(rovnaký vzor ako `spotreba._hourly`). Mená izieb: `common.ROOM_NAMES` (+ „Chodba“ pre chodba).

## Backend – nový modul `vonku.py` + route v `app.py`
`report(cfg, od=None, do=None, krok=2, now=None) -> dict`:
- `od`/`do` = 'YYYY-MM-DD' vrátane (cez `stats._pdate`, hranice cez `stats._midnight`); None = od = najstarší záznam, do = dnes.
  Neplatný vstup / do < od → ValueError so slovenským textom. `krok` ∈ {1, 2, 3, 5} (°C), iné → ValueError.
- Jedným SELECTom načítaj hodiny v rozsahu, kde `vonku IS NOT NULL`.
- Pásmo: `lo = floor(vonku / krok) * krok`, pásmo [lo, lo+krok). Pásma s menej než 3 hodinami vynechaj.
- Pre každé pásmo: `od_c` (lo), `do_c` (lo+krok), `hodin`, `vonku_avg`, a pre každý stĺpec z
  `["obyvacka","detska","jedalen","kuchyna","spalna","pracovna","kupelna","chodba","tw2","tw1","wl"]`:
  `{"min", "avg", "max", "n"}` (z hodín, kde stĺpec nie je NULL; n < 3 → None celé). Zaokrúhli na 1 desatinné miesto.
  Ďalej `kwh_h` = priemer čerpadla kWh/hod (COALESCE(kwh_cal, kwh), len hodiny s údajom, ≥3, na 2 des. miesta) a `beh_pct` = priemer
  beh_min/60·100 (≥3 hodín, celé číslo).
- Pre min a max pridaj aj kedy nastali: `min_ts`, `max_ts` (ts hodiny; pri zhode najnovšia).
- Výstup: `{"od", "do", "krok", "pasma": [...] (zoradené od najchladnejšieho), "stlpce": [{"key","name","skupina":"izba"|"voda"}],
  "hodin_spolu", "prvy_den", "posledny_den"}`. Názvy vody: tw2 „Voda výstup“, tw1 „Spiatočka“, wl „Cieľ krivky“.
  Stĺpce, ktoré nemajú v žiadnom pásme údaje, z `stlpce` vynechaj.
- `app.py`: GET `/api/vonku?od=&do=&krok=` → 400 s textom pri ValueError; pridaj do `USER_GET`. Do 1 s pre ~10 000 hodín.

## Frontend – `ui.html` (záložka Izby)
Prečítaj si, ako je spravená záložka Spotreba (segmentové tlačidlá `.subseg`, tabuľky v `.scroll`, prefix `sp`, localStorage s try/catch)
a dodrž rovnaký štýl, CSS premenné, tmavý/svetlý režim. Prefix pre nové id/premenné: `vk`.
1. V hlavičke karty „Izby“ (`#roomlist .head`) tlačidlo `button.btn.sm#vkbtn` „Podľa vonkajšej teploty“. Klik zobrazí novú sekciu
   `section.panel.pad#vkbox` (pod zoznamom izieb; zoznam izieb ostáva) a posunie k nej; opätovný klik / tlačidlo „Zavrieť“ v jej hlavičke
   ju skryje. Stav (otvorené, obdobie, krok) pamätaj v localStorage. Detail izby (`#roomdetail`) sekciu skryje rovnako ako zoznam.
2. Hlavička sekcie: „Teploty podľa vonkajšej teploty“ + podnadpis s rozsahom dátumov a počtom hodín.
   Segmenty obdobia: „Táto zima“ (od 1. 10. aktuálnej sezóny – ak je dnes pred 1. 10., od 1. 10. minulého roka; predvolené),
   „30 dní“, „Minulá zima“ (1. 10. – 30. 4. predošlej sezóny), „Všetko“. Segmenty kroku: „1 °C“, „2 °C“ (predvolené), „5 °C“.
   Prepínač „Izby / Voda“ (čo je v tabuľke; Voda len ak sú vodné stĺpce).
3. **Tabuľka**: riadok = pásmo vonkajšej teploty, najchladnejšie hore; prvý stĺpec „Vonku“ ako „−6 až −4 °C“ (skutočné mínus „−“,
   desatinná čiarka), druhý „Hodín“. Ďalej po stĺpci na izbu (alebo vodu): v bunke `min – max` väčším písmom a pod tým malým
   „Ø 22,4“. Title/tooltip bunky: „najnižšia 21,3 °C (12. 1. 6:00), najvyššia 23,9 °C (…)“. Pri vode navyše stĺpce „Čerpadlo kWh/h“
   a „Beh %“. Chýbajúce = „–“.
   Farebné zvýraznenie čísla min (len izby, nie chodba): < 21,0 modrastá (chladno), max > 24,5 teplá – použi existujúce
   premenné/paletu (nájdi stavové farby v CSS), nie nové natvrdo farby, a nech je čitateľné v oboch režimoch.
   Hlavička tabuľky „sticky“ prvý stĺpec, aby sa na mobile dalo vodorovne posúvať len vo vnútri `.scroll`.
4. Pod tabuľkou malým písmom: „Hodnoty sú z hodinových priemerov (krátke výkyvy v rámci hodiny nie sú vidieť). Pásmo ‚−6 až −4‘ znamená
   vonku od −6 do −4 °C. Pásma s menej než 3 hodinami sú vynechané.“
5. Nad tabuľkou krátky automatický prehľad (max 3 riadky), napr. „Pri vonku pod 0 °C: najchladnejšia izba Spálňa (min 20,1 °C),
   najteplejšia Kuchyňa (max 24,8 °C).“ a to isté pre 0–10 °C – vypočítaj z dát v JS (min z min, max z max cez pásma), bez chodby.
Dáta sa načítajú pri otvorení sekcie a pri zmene obdobia/kroku (`/api/vonku?od=&do=&krok=`). Žiadne externé knižnice.

## Testy – `test_vonku.py` (unittest)
Dočasná stats.db: pásmovanie (aj záporné hodnoty: −0,5 patrí do [−2, 0) pri kroku 2), min/max/avg a min_ts/max_ts, vynechanie
pásma < 3 hodín, stĺpec bez údajov sa vynechá, chyba pri zlom kroku a do < od, chýbajúca DB → prázdne pásma.

## Odovzdanie
Zoznam zmenených súborov, výstup testov (`python -m unittest test_vonku test_spotreba`), ako si overil syntax JS v ui.html.
