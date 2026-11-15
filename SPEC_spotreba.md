# SPEC: záložka „Spotreba“ – denná spotreba a odhad ceny (14. 11. 2026)

Cieľ: v aplikácii (ui.html) nová záložka **Spotreba** (`#spotreba`), kde používateľ vidí pre každý deň, koľko elektriny
minul celý dom a z toho tepelné čerpadlo, a odhadovanú cenu podľa cenníka. Len štandardná knižnica Pythonu, texty po slovensky.
Nič nenasadzuj, na server nechoď.

## Zdroje dát (už existujú)
- `stats.db`, tabuľka `hourly` (stats.py): `ts` (začiatok hodiny, unix), `kwh_dom` (celý dom z elektromera, chodí s denným
  oneskorením), `kwh_cal` / `kwh` (čerpadlo, kalibrovaný výpočet / odhad; čerpadlo od 7. 11. 2026), `q_kwh` (teplo), `vonku`.
  Otvárať len na čítanie: `stats._ro(stats._db_path(cfg), timeout)`.
- `ims_hourly.json` (`ha_cost.load_ims()` → `{ts_hodiny: kWh}`): história celého domu od 15. 11. 2023 z portálu distribúcie.
  Kde má `stats.db` hodnotu `kwh_dom`, má prednosť; inak sa použije ims_hourly.
- Cena: `ha_cost.tariff_at(cfg, ts)` → `(vážená cena €/kWh s DPH, fixná platba €/hod)`; `ha_cost.TARIFF` (cenníky s dátumom
  platnosti), config `ENERGY_ENERGOPOMOC`, `ENERGY_VT_SHARE`, `ENERGY_FEE_MONTH`.
- Fakturačné obdobie: `ha_pub.billing_start(cfg, now)` (začiatok obdobia, unix).

## Backend – nový modul `spotreba.py` + route v `app.py`
`report(cfg, od, do, now=None) -> dict` (čistá logika; od/do = 'YYYY-MM-DD' vrátane; max 400 dní, inak chyba):
- Hodiny lokálneho dňa (pozor na zmenu času 23/25 h – deň počítaj cez lokálnu polnoc ako v stats.py `_bounds`).
- Pre každý deň: `datum`, `den_tyzdna` (po, ut, …), `dom_kwh` (súčet, None ak žiadna hodina), `dom_hodin` (počet hodín s údajom),
  `kompletny` (dom_hodin == počet hodín dňa), `cerpadlo_kwh` (súčet COALESCE(kwh_cal, kwh), None ak nič), `ostatne_kwh`
  (dom − čerpadlo, len ak sú obe a čerpadlo má aspoň 20 hodín; inak None), `teplo_kwh`, `cop` (teplo / čerpadlo, ak čerpadlo ≥ 0,5),
  `vonku` (priemer), `cena_eur` = Σ_h (kWh_h × cena_h) + Σ_h poplatok_h cez všetky hodiny dňa (poplatok aj za hodiny bez údaja,
  ale len ak má deň aspoň 1 hodinu spotreby; inak None), `cena_cerpadlo_eur` = Σ (čerpadlo_h × cena_h).
  Zaokrúhli kWh na 2, € na 2 desatinné miesta.
- `spolu`: súčty za obdobie (dom, čerpadlo, ostatné len zo dní kde sú obe, cena, cena_cerpadlo), `dni_s_udajmi`, `priemer_den_kwh`,
  `priemer_den_eur` (z dní s údajmi), `odhad_mesiac_eur`/`odhad_obdobie_eur` netreba.
- `mesiace`: pre každý kalendárny mesiac v rozsahu rovnaké súčty (dom, čerpadlo, cena, dni_s_udajmi).
- `cennik`: `{vazena_cena, poplatok_den, vt, nt, mesacna_platba, energopomoc (bool), vt_podiel, platny_od}` pre posledný deň rozsahu
  (vt/nt/mesačná platba z riadku TARIFF, ktorý v ten deň platí; pozor, ENERGY_FEE_MONTH ho prepíše).
- `posledny_den_s_udajmi` (dom), `obdobie_zaciatok` (fakturačné obdobie, 'YYYY-MM-DD').
`app.py`: GET `/api/spotreba?od=&do=`; bez parametrov = od začiatku aktuálneho fakturačného obdobia po dnes. Chyba vstupu → 400
s textom. Pridaj `/api/spotreba` do `USER_GET` (vidí ju aj rola user). Výpočet nesmie trvať > 1 s pre 400 dní (načítaj
hodiny jedným SELECTom za rozsah, ims_hourly.json načítaj raz – môžeš ho cache-ovať podľa mtime).

## Frontend – `ui.html`
Prečítaj si, ako sú v ui.html spravené záložky (hash routing, `#prehlad`, `#den`, …), karty, dlaždice (stat), tabuľky, grafy
(SVG, paleta, spoločné CSS premenné, tmavý/svetlý režim) a dodrž rovnaký štýl. Nová záložka **Spotreba** v hlavnom menu hneď za
„Prehľad“; viditeľná aj pre rolu user.
1. **Výber obdobia** (segmentové tlačidlá): „Fakturačné obdobie“ (predvolené), „Tento mesiac“, „Minulý mesiac“, „30 dní“,
   a mesiac so šípkami ◀ ▶ (prechádzanie mesiacov dozadu až do 11/2023). Pamätaj voľbu v localStorage (try/catch).
2. **Dlaždice**: Spotreba domu (kWh), Odhad ceny (€), Tepelné čerpadlo (kWh a % z domu, ak sú údaje), Priemer za deň (kWh a €).
   Pod dlaždicami jeden riadok malým písmom: „Údaje z elektromera sú do DD. MM. (posielajú sa s denným oneskorením). Cena: DD5,
   vážene X € / kWh (VT … / NT …, Y % vo VT) + Z € mesačne, s DPH, s/bez adresnej energopomoci. Je to odhad, nie faktúra.“
3. **Graf po dňoch**: stĺpce = spotreba domu; ak je čerpadlo, stĺpec rozdelený na čerpadlo (spodná časť) a ostatné; neúplný
   deň vyznač (napr. šrafovanie alebo nižšia sýtosť) – tooltip pri prejdení/ťuknutí: dátum, dom, čerpadlo, ostatné, cena, vonku.
   Os Y v kWh. Na mobile sa graf musí zmestiť (pri viac ako ~45 dňoch zúž stĺpce, bez horizontálneho posúvania stránky).
4. **Tabuľka po dňoch** (najnovší deň hore): Dátum (s dňom v týždni), Dom kWh, Čerpadlo kWh, Ostatné kWh, Vonku °C, Cena €.
   Chýbajúce = „–“. Neúplný deň označ (napr. „čiastočne“). Riadok súčtu dole. Čísla s desatinnou čiarkou.
5. Ak rozsah obsahuje viac mesiacov: malá tabuľka **Po mesiacoch** (mesiac, dom kWh, čerpadlo kWh, cena €).
Dáta sa načítajú pri otvorení záložky a pri zmene obdobia (`/api/spotreba?od=&do=`). Žiadne nové externé knižnice.

## Testy – `test_spotreba.py` (unittest)
Dočasná stats.db s niekoľkými dňami, ims slovník, monkeypatch cesty k ims; overiť: prednosť stats.db pred ims, deň so zmenou
času (25 h), cena = Σ kWh × cena + poplatok, neúplný deň, ostatné = dom − čerpadlo, mesačné súčty, chyba pri > 400 dňoch.

## Odovzdanie
Zoznam zmenených súborov, výstup testov, čo si nevedel overiť (UI nespúšťaš v prehliadači – len over syntax JS, napr. tak, že
vytiahneš skript z ui.html a spustíš ho cez `python -c` s jednoduchou kontrolou zátvoriek, alebo opíš, ako si to overil).
