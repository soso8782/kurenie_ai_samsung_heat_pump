# SPEC: „Ako kúri“ – teplota vody podľa vonkajšej teploty (15. 11. 2026, doplnok k SPEC_vonku.md)

Používateľ chce vidieť, **na akú teplotu vody tepelné čerpadlo kúri pri akej vonkajšej teplote – najnižšia a najvyššia**.
Existujúca sekcia `#vkbox` (záložka Izby, tlačidlo `#vkbtn`, vonku.py, `/api/vonku`) ostáva, ale hlavný a predvolený pohľad bude
nový „Ako kúri“. Len štandardná knižnica, texty po slovensky. Nič nenasadzuj, na server nechoď.

## Dáta – tc.db (minútové vzorky od 7. 11. 2026)
`samples(ts, entity, value)` (každá minúta, index na ts), `events(ts, kind, entity, old, new, text)`. Otvor len na čítanie
(vzor `stats._tc(cfg)` alebo `common.tcq`). Entity (názvy presne):
- „Vonkajšia teplota“, „Výstupná teplota vody (Tw2)“, „Spiatočka (Tw1)“, „Cieľová ekvitermická voda (WL)“, „Frekvencia kompresora“,
  „Posun ekvitermickej krivky“
- nastavenia krivky (berú sa posledné hodnoty ≤ koniec rozsahu): „FSV 2011 Vonku najchladnejšie“ (−15), „FSV 2012 Vonku najteplejšie“ (15),
  „FSV 2031 WL2 voda pri najchladnejšom“ (46), „FSV 2032 WL2 voda pri najteplejšom“ (35), „FSV 4013 Vypnutie kúrenia nad vonk. teplotou“ (17)
- 3-cestný ventil a odmrazovanie z `events`: použi `stats._tracks(tc, a, b)` → `(valve, defr)`; `valve.at(ts)` = "room"/"tank"/None,
  `defr.at(ts)` = True/False/None.

## Backend – `vonku.py`: nová funkcia `kurenie(cfg, od=None, do=None, krok=1, now=None) -> dict`
- od/do ako v `report` (None = od prvej vzorky po dnes); krok ∈ {1, 2, 5}; chyby ValueError po slovensky. Max 400 dní.
- Načítaj vzorky uvedených entít v rozsahu jedným SELECTom (`entity IN (...)`), zoskup po minúte (`ts // 60`). Vonkajšiu teplotu
  dopĺňaj poslednou známou hodnotou (max 15 min stará).
- **Minúta kúrenia** = frekvencia > 0, ventil "room", nie odmrazovanie, a kompresor beží nepretržite aspoň 10 minút (predošlých 10 minút
  frekvencia > 0 – vynechá rozbeh) a do 10 min po odmrazovaní nie (ak odmrazovanie skončilo menej než pred 10 min, minútu vynechaj).
- **Minúta v režime kúrenia** (pre podiel behu) = ventil nie "tank", nie odmrazovanie (kompresor beží alebo stojí).
- Pásmo podľa vonku: `lo = floor(vonku / krok) * krok`. Pre každé pásmo s aspoň 30 minútami kúrenia:
  `od_c`, `do_c`, `minut_kurenia`, `vonku_avg`,
  `tw2` {min, avg, max, min_ts, max_ts} – výstupná voda v minútach kúrenia (min/max ako 5. a 95. percentil, aby jednotlivé
  výkyvy neskresľovali; pridaj aj `abs_min`, `abs_max` skutočné krajné hodnoty),
  `tw1` {min, avg, max} (rovnako percentily), `wl` {min, avg, max} (cieľ krivky vrátane posunu, v minútach kúrenia),
  `posun` {min, max} (v minútach režimu kúrenia), `beh_pct` = minúty s frekvenciou > 0 / minúty v režime kúrenia × 100 (celé číslo),
  `krivka` = voda podľa nastavenej krivky bez posunu v strede pásma (lo + krok/2): lineárne medzi (FSV 2011, FSV 2031) a
  (FSV 2012, FSV 2032), mimo intervalu orezané na krajnú hodnotu; 1 desatinné miesto.
  Pásma nad FSV 4013 označ `nad_vypnutim: true`.
- Zaokrúhľuj na 1 desatinné miesto. Výstup: `{"od","do","krok","pasma":[...] (od najchladnejšieho),
  "nastavenie": {"fsv2011","fsv2012","fsv2031","fsv2032","fsv4013"}, "prva_vzorka", "minut_spolu"}`.
- Rýchlosť: 30 dní vzoriek (6 entít × 43 200) do 2 s.
- `app.py`: GET `/api/akokuri?od=&do=&krok=` (400 pri ValueError), pridaj do `USER_GET`.

## Frontend – `ui.html`, sekcia `#vkbox`
- Tlačidlo `#vkbtn` premenuj na **„Ako kúri podľa vonku“**, nadpis sekcie „Ako kúri podľa vonkajšej teploty“.
- Prepínač pohľadu (dnes Izby/Voda) nahraď tromi: **„Voda“** (nový, predvolený), „Izby“ (existujúca tabuľka izieb), a starý
  „Voda (hodinovo)“ zruš. Pamätaj v localStorage ako doteraz (neplatnú/starú hodnotu nahraď predvolenou).
- Pre pohľad „Voda“ volaj `/api/akokuri`; segmenty obdobia rovnaké (Táto zima / 30 dní / Minulá zima / Všetko) a krok 1 / 2 / 5 °C
  (predvolený 1 °C pre tento pohľad; 2 °C pre Izby – každý pohľad má vlastný krok v localStorage).
- **Tabuľka „Voda“** (najchladnejšie hore): „Vonku“ (−3 až −2 °C) · „Podľa krivky“ (napr. 43,2 °C) · **„Voda do radiátorov“**
  = `min – max` veľkým písmom, pod tým malým „Ø 39,1“ (title: „najnižšia … (dátum čas), najvyššia … ; krajné hodnoty …“) ·
  „Cieľ s posunom“ min – max · „Spiatočka“ Ø · „Posun“ min až max (napr. „−1,5 až +2“) · „Beh %“ · „Čas“ (minúty kúrenia ako „5 h 20 min“).
  Pásma nad vypnutím kúrenia sivou s poznámkou. Desatinná čiarka, mínus „−“.
- **Graf nad tabuľkou** (SVG, rovnaký štýl ako ostatné grafy v ui.html, CSS premenné, oba režimy): os X vonkajšia teplota, os Y °C vody.
  Pre každé pásmo zvislý pruh od min po max výstupnej vody so značkou priemeru, a čiara nastavenej krivky (bez posunu) cez celý rozsah osi X
  (od FSV 2011 po FSV 2012, orezať na zobrazený rozsah ±2 °C okolo dát). Legenda: „Voda do radiátorov (min–max, Ø)“, „Nastavená krivka“.
  Tooltip/ťuknutie na pruh: hodnoty pásma. Na mobile sa zmestí (bez vodorovného posúvania stránky).
- Krátky prehľad nad grafom (1–2 vety z dát): napr. „Pri −3 °C vonku kúri na 38–44 °C (Ø 41), podľa krivky má 43,2 °C.“ pre najchladnejšie
  pásmo a pre pásmo okolo 5 °C, ak existuje.
- Poznámka pod tabuľkou: „Len minúty, keď kompresor kúri do radiátorov aspoň 10 min (bez ohrevu TÚV, odmrazovania a rozbehu). Min a max sú
  bez krajných 5 % výkyvov. Krivka: FSV 2031/2032 = 46/35 °C pri −15/+15 °C, firmvér ju posúva podľa izbovej teploty (stĺpec Posun).
  Minútové údaje sú od 7. 11. 2026.“ (čísla z `nastavenie`).

## Testy – doplň do `test_vonku.py`
Dočasná tc.db so samples/events: minúty TÚV (ventil tank) a odmrazovanie sa nerátajú, rozbeh (<10 min) sa nerátá, pásmovanie,
percentily 5/95 aj abs_min/abs_max, krivka (interpolácia + orezanie), beh_pct, pásmo < 30 min vynechané, chýbajúca DB → prázdne.

## Odovzdanie
Zmenené súbory, výstup `python -X utf8 -m unittest test_vonku test_spotreba`, overenie syntaxe JS, čo si neoveril.
(Python: `C:\Program Files\KiCad\10.0\bin\python.exe`; node nie je – syntax JS over ako minule cez Edge headless.)
