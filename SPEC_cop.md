# SPEC: presnejšia spotreba a COP (14. 11. 2026)

Mení sa `stats.py` (+ nový `test_cop.py`, drobný text v UI). Len štandardná knižnica Pythonu. Komentáre a texty po slovensky.
Nič nenasadzuj, na server nechoď. Existujúce testy (`t_energia.py`) musia ďalej prechádzať (uprav ich len tam, kde sa zámerne mení správanie).

## Zistenia z dát (dôvod zmien)
- Jednotka neposiela prietok ani príkon. Príkon = odhad z prúdu kompresora (počítadlo „Denná spotreba TČ“), teplo = prietok × ΔT.
- V noci (0–5 h) je spotreba domu stabilná: `kwh_dom ≈ 0,08 + 1,02 × kwh`, chyba 0,05 kWh/h. Denné hodiny sú zašumené spotrebou domu.
- Z tabuliek výrobcu (AE120JXED*H, Peak = bez odmrazovania) vychádza v ustálenom chode prietok ≈ 22,5 l/min pri kúrení a ≈ 42 l/min
  pri ohreve TÚV. Jeden pevný prietok 20 l/min pre oba okruhy dával pri TÚV polovičný COP.

## 1. Kalibrácia `k` z nočných hodín — `calibration_calc(rows, now)`
Podpis a doterajšie kľúče výstupu ostávajú. Doterajší výpočet základu domu (`base`, `base_kwh`, `base_zdroj`) ostáva bez zmeny.
Nové, pred doterajším výpočtom `k`:
- nočné hodiny = lokálna hodina 0–4 (`tm_hour < 5`), posledných `CAL_DAYS` dní, `kwh_dom` nie je None, `beh_min >= 20`, `kwh >= 0.3`.
- podmienky: aspoň `NIGHT_MIN_N = 12` takých hodín z aspoň `CAL_MIN_DAYS` rôznych dní a rozpätie `kwh` (max − min) >= 0,8.
- `k` = Theil–Sen sklon (medián sklonov dvojíc s |Δkwh| > 0,3; pri > 200 bodoch rovnomerne preriediť), `b0` = medián(dom − k·kwh).
  Urob z `_ts_intercept` spoločnú funkciu `_theil_sen(pts, max_pts=200, min_dx=0.15) -> (b1, b0) | None`; `_ts_intercept` nech ju volá.
- výsledok: `k_raw`, `k` (orezané K_LO..K_HI + `dovod` ako doteraz), `mape` = priemer |b0 + k·kwh − dom| / dom × 100 na nočných hodinách,
  `n_hodin` = počet nočných hodín, `n_dni` = počet dní, nové kľúče `metoda = "noc"`, `noc_zaklad_kwh = round(b0, 3)`.
- ak podmienky nie sú splnené: doterajší výpočet (medián denných podielov), `metoda = "dni"`, `noc_zaklad_kwh = None`.
- `base_kwh` atď. sa vyplní v oboch prípadoch (ak sa základ dá určiť); ak sa základ určiť nedá, ale nočná metóda prešla, `k` sa aj tak vráti.

## 2. Tabuľka výrobcu a prietok podľa okruhu
Konštanty v `stats.py`:
```
TAB_TA = (-20, -15, -10, -7, -2, 2, 7, 10, 15, 20)
TAB_LW = (25, 30, 35, 40, 45, 50, 55)
TAB_PEAK = {   # AE120JXED*H/EU, Maximum Heating Capacity (Peak Value): (HC kW, PI kW); None = mimo rozsahu
 -20: ((9.84, 3.38), (9.55, 3.91), (9.26, 4.45), (8.96, 4.99), None, None, None),
 -15: ((10.73, 3.38), (10.45, 3.91), (10.17, 4.45), (9.90, 4.99), (9.63, 5.53), None, None),
 -10: ((11.27, 3.33), (11.17, 3.85), (11.25, 4.37), (11.02, 4.90), (10.97, 5.44), (10.93, 5.97), (10.92, 6.48)),
 -7: ((11.59, 3.30), (11.61, 3.81), (11.64, 4.33), (11.70, 4.85), (11.77, 5.38), (11.87, 5.91), (12.01, 6.44)),
 -2: ((12.55, 3.02), (12.55, 3.50), (12.56, 3.97), (12.59, 4.45), (12.64, 4.93), (12.71, 5.42), (12.81, 5.90)),
 2: ((13.06, 2.68), (13.06, 3.11), (13.07, 3.53), (13.09, 3.96), (13.13, 4.39), (13.18, 4.83), (13.26, 5.26)),
 7: ((12.50, 1.95), (12.25, 2.27), (12.00, 2.59), (11.75, 2.91), (11.50, 3.23), (11.25, 3.55), (11.00, 3.87)),
 10: ((13.05, 1.97), (12.81, 2.29), (12.58, 2.61), (12.34, 2.93), (12.10, 3.25), (11.86, 3.57), (11.62, 3.89)),
 15: ((13.97, 2.00), (13.75, 2.32), (13.54, 2.64), (13.32, 2.96), (13.10, 3.29), (12.88, 3.61), (12.66, 3.93)),
 20: ((14.89, 2.03), (14.69, 2.35), (14.50, 2.68), (14.30, 3.00), (14.10, 3.32), (13.90, 3.64), (13.70, 3.96)),
}
```
- `cop_tab(tamb, lwt)`: bilineárna interpolácia HC/PI; vstupy orezať na rozsah tabuľky; bunka None = posledná platná v riadku.
  Kontrola: `cop_tab(7,35)` ≈ 4.63, `cop_tab(2,35)` ≈ 3.70, `cop_tab(0,37)` ≈ 3.29, `cop_tab(0,47)` ≈ 2.68.
- `flow_calc(samples, valve, defr_int, k) -> dict` (čistá funkcia, testovateľná): `samples` = slovník kľúč → {minúta_ts: hodnota} pre
  `freq, tw2, tw1, prikon, vonku`; `valve` = objekt s `.at(ts)` → "room"/"tank"/None; `defr_int` = zoznam (začiatok, koniec) odmrazovaní;
  `k` = kalibračný koeficient (None → 1.0). Ustálená minúta `t`:
  kompresor beží v `t` aj v 15 predošlých minútach (freq > 0, chýbajúca vzorka = nebeží); žiadne odmrazovanie v intervale
  [začiatok − 600 s, koniec + 900 s]; ventil v `t` aj `t − 600` rovnaký a je "room" alebo "tank"; ΔT = tw2 − tw1 >= 1,5; príkon >= 300 W.
  Prietok minúty = `cop_tab(vonku, tw2) × prikon × k / 1000 / (CP_WATER × ΔT / 60)` [l/min].
  Výsledok: `{"kurenie": medián|None, "tuv": medián|None, "n_kurenie": n, "n_tuv": n, "p25_kurenie", "p75_kurenie", "p25_tuv", "p75_tuv"}`;
  medián sa vráti len pri n >= `FLOWCAL_MIN_ROOM = 300` (kúrenie) resp. `FLOWCAL_MIN_TANK = 60` (TÚV) a ak je v rozsahu 8–60 resp. 10–90,
  inak None. Zaokrúhliť na 1 desatinné miesto.
- `flow_calibrate(cfg, now=None)`: načíta z tc.db minútové vzorky za `FLOWCAL_DAYS = 30` dní (ts zarovnať na minútu), stopy ventilu a
  odmrazovania cez `_tracks`, zavolá `flow_calc`, doplní `ts`, `datum`, `zdroj = "tabuľky výrobcu (AE120JXED, bez odmrazovania)"`,
  uloží JSON do meta pod kľúč `prietok` a vráti slovník. Nikdy nevyhodí výnimku (pri chybe vráti {"error": ...} a meta nemení).
- `get_flows(con) -> {"room": lpm|None, "tank": lpm|None}` z meta `prietok` (None, ak chýba alebo je nezmysel).

## 3. Použitie prietoku — poradie priority pre každú minútu
1. platná hodnota senzora „Prietok vody“ (`flow_ok`),
2. zmerané hodnoty z config.env: `STATS_FLOW_LPM` (kúrenie) a `STATS_FLOW_TUV_LPM` (TÚV); ak je nastavené len `STATS_FLOW_LPM`,
   platí len pre kúrenie (TÚV ide ďalej na bod 3),
3. automaticky kalibrovaný prietok okruhu z meta `prietok` (podľa polohy ventilu v danej minúte; neznáma poloha = kúrenie),
4. inak minúta nemá prietok (ako doteraz).
Zaveď pomocnú `flow_for(st, sensor, cfg_flows, cal_flows)`; `cfg_flow(cfg)` nahraď `cfg_flows(cfg) -> {"room":..,"tank":..}`
(ak `cfg_flow` používa niekto iný – app.py, testy – ponechaj ju ako tenký obal vracajúci hodnotu pre kúrenie).

`pth_kw(flow, tw2, tw1)`: **zruš orezanie na nulu** – pri odmrazovaní je ΔT záporné a teplo sa z domu odoberá, má sa odpočítať.
Hodinové `q_kwh` nesmie byť záporné vo výsledku: `max(0, …)` až na úrovni hodiny. V `live()`: `p_th_kw` a `cop_now` nech sú None,
ak je súčet tepla za 15 min <= 0.

`_agg_chunk(cfg, tc, a, b, last_ts, kcal=None, flows=None)`: `flows` = výsledok `get_flows`; volajúci (`aggregate_range`, `catch_up`,
`recompute_range` – všade, kde sa dnes číta `get_k`) ho načítajú rovnako ako `kcal`. `live()` tiež (potrebuje stopu ventilu: `_tracks`).
`live()` nech vracia navyše `flow_lpm` (prietok použitý v poslednej minúte) a `flow_zdroj` ("senzor" | "zmerany" | "tabulky" | None).

## 4. Prepočet
`recalibrate(cfg, now)`: po výpočte `k` zavolaj `flow_calibrate` (s novým `k`). Ak sa `k` zmenilo o > 0,005 alebo niektorý prietok
o > 0,3 l/min oproti predošlým hodnotám v meta (alebo predtým chýbali), prepočítaj hodinové riadky za obdobie, kde sú minútové dáta
v tc.db (najviac 60 dní) – použi existujúci `recompute_range`/`aggregate_range`; pozor na zámky DB a na to, aby prepočet neprepísal
`kwh_dom`. Výsledok `recalibrate` doplň o kľúč `prietok` (slovník z `flow_calibrate`). Do JSON `kalibracia` v meta to nedávaj dvakrát.
`coverage()` / `get_day()` / kdekoľvek sa dnes vracia `kalibracia`: pridaj aj `prietok` (slovník z meta alebo None).

## 5. UI
Nájdi, kde sa v `ui.html` / `stats_ui.js` zobrazuje informácia o kalibrácii spotreby (hľadaj „kalibr“). Doplň tam stručne:
„Prietok: kúrenie 22,5 l/min · TÚV 42,0 l/min (z tabuliek výrobcu)“ resp. „(zmeraný)“ a pri metóde `noc` text
„kalibrované podľa nočných hodín elektromera“. Žiadne nové karty, len text v existujúcom mieste. Ak `stats_ui.js` lokálne nie je,
napíš to do správy a UI nerob.

## 6. Testy — `test_cop.py` (unittest, spustiteľné `python test_cop.py`)
- `cop_tab` kontrolné body (±0,02), orezanie mimo rozsahu, bunky None.
- `calibration_calc`: syntetické nočné hodiny `dom = 0.1 + 1.02·kwh` + denné hodiny s veľkým šumom → `metoda == "noc"`, k ≈ 1.02 (±0,01);
  málo nočných hodín → `metoda == "dni"` a rovnaký výsledok ako pred zmenou.
- `flow_calc`: syntetický chod s prietokom 25 l/min (kúrenie) a 40 l/min (TÚV) vygenerovaný cez `cop_tab` → vráti 25 a 40 (±0,2);
  minúty pri odmrazovaní a prvých 15 min po štarte sa nepoužijú; málo minút → None.
- `flow_for` priorita; `pth_kw` záporné pri zápornom ΔT; hodinové `q_kwh` >= 0.
- `_agg_chunk` na dočasnej tc.db: hodina s 30 min kúrenia a 30 min TÚV použije dva rôzne prietoky.

## Odovzdanie
Zoznam zmenených súborov, výstup testov (`test_cop.py`, `t_energia.py`), a čo si urobil inak než v špecifikácii a prečo.
