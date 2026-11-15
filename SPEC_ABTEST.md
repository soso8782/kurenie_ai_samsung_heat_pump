# Porovnávací test A/B – koľko šetrí regulácia podľa izby (špecifikácia)

Cieľ: zmerať, o koľko percent menej energie spotrebuje dom s reguláciou podľa izby (firmvér ESP posúva krivku podľa detskej
izby = režim **A**) oproti samotnej ekvitermickej krivke Samsungu bez spätnej väzby z izby (režim **B** = to, čo by robila
„automatika“ tepelného čerpadla). Test spúšťa VÝLUČNE používateľ tlačidlom na webe. Nič sa nespúšťa samo.

## Ako sa prepína režim
Vo firmvéri m5stack-tc je select **„Režim izbovej regulácie“** s možnosťami `Vypnutá`, `Posun krivky`, `Vypínanie kúrenia`.
- A = `Posun krivky` (bežná prevádzka), B = `Vypnutá`. Firmvér pri prechode do `Vypnutá` sám vráti posun krivky na 0.
- `Vypínanie kúrenia` sa NIKDY nepoužije.
- Zápis: `POST {ESP_URL}/select/<url-encoded názov>/set?option=<url-encoded možnosť>` (ESPHome web_server), overenie čítaním
  `GET {ESP_URL}/select/<názov>` → pole `value`.
- Nová funkcia `agent.esp_set_select(cfg, name, option)`: povolená LEN dvojica názov = „Režim izbovej regulácie“ a možnosť
  ∈ {„Vypnutá“, „Posun krivky“}, inak `ValueError`. Rešpektuje DRY_RUN rovnako ako `esp_set_number` (nič neodošle, len zaloguje).

## Plán testu
- Dĺžka: 10 dní (voliteľne 6, 8, 10, 14 – párne číslo). Začína o polnoci najbližšieho dňa po stlačení „Spustiť“ (nikdy hneď).
- Poradie režimov po dňoch je vyvážené proti trendu počasia: pre 10 dní `A B B A A B B A A B`; všeobecne opakuj blok `A B B A`
  a orež na dĺžku. Prvý deň vždy A.
- Prepnutie na režim dňa prebieha o 00:00 (prvá minúta dňa). Pre vyhodnotenie sa každý deň berie okno **04:00 – 24:00**
  (prvé 4 h sú prechod, lebo dom má zotrvačnosť).

## Stav (v `agent.db`, tabuľka `state`, kľúč `abtest`, JSON)
```
{"status": "planned|running|done|stopped",
 "created": ts, "start": "YYYY-MM-DD", "days": [{"date": "YYYY-MM-DD", "mode": "A|B", "valid": true, "note": ""}],
 "switched_by_test": bool,            # test práve držal ESP v režime B
 "stopped_reason": str|null, "result": {...}|null}
```
`planned` = čaká na prvú polnoc, `running` = beží, `done` = skončil, `stopped` = zastavený (používateľom alebo poistkou).

## Logika (nový modul `abtest.py`, `tick(cfg, c, now)` volá strážca každú minútu, všetko v try/except)
1. Ak status ∈ {planned, running} a dnešný dátum je v pláne → požadovaný režim = režim dňa; inak (pred začiatkom) nič.
   Ak je dnešný dátum za posledným dňom → ukonči: režim A, status `done`, vypočítaj výsledok, upozornenie `abtest`.
2. Ak požadovaný režim ≠ aktuálna hodnota selectu v ESP → zapíš ho (najviac 1 pokus za 5 min; po 3 neúspešných pokusoch za deň
   zastav test s dôvodom). Pri B nastav `switched_by_test=true`, pri A `false`. Pri prvom prepnutí zmeň status na `running`.
3. **Poistky počas dňa B** (priemer posledných 30 min z tc.db): detská > 25,0 °C alebo < 22,0 °C, chybový kód TČ ≠ 0,
   zapnutý elektrický dohrev kúrenia, dáta zberača staršie ako 15 min → okamžite prepni na A, deň označ `valid=false`
   s poznámkou, upozornenie `abtest`. Zvyšok dňa ostáva A (deň sa nevyhodnocuje).
4. **Nikdy nenechať reguláciu vypnutú:** ak status nie je `running`/`planned` (stopped, done, žiadny test) a `switched_by_test`
   je true → okamžite zapíš A a nastav false. To isté pri štarte agenta.
5. Zastavenie z webu: status `stopped`, `stopped_reason` = „zastavené používateľom“, hneď režim A, výsledok z doteraz
   ukončených dní (ak sú aspoň 2 platné dni A a 2 platné dni B).
6. Ak používateľ zmení select ručne (hodnota v ESP ≠ to, čo test naposledy zapísal, mimo okna prepínania 00:00–00:05),
   test sa zastaví s dôvodom „režim regulácie zmenený ručne“ a režim sa už neprepisuje.

## Vyhodnotenie (`evaluate(cfg, st)`)
Pre každý deň okno 04:00–24:00 cez `hour_stats` (len hodiny s dátami, deň platí ak má ≥ 16 h dát a `valid`):
- `kwh` = súčet hodinových kWh (odhad z prúdu – rovnaká chyba v oboch režimoch, preto vhodné na relatívne porovnanie),
- `t_in` = priemer izieb detská, jedáleň, kuchyňa, obývačka, spálňa, pracovňa, kúpeľňa (priemer hodinových priemerov),
- `t_out` = priemer vonku, `dT = t_in − t_out`,
- `ua` = kwh / (dT × hodiny) … spotreba na 1 °C rozdielu vnútra a vonku za hodinu (hlavná metrika, nezávisí od počasia ani od toho,
  že v B môže byť v izbách teplejšie),
- komfort: hodiny detskej mimo 23,0–24,0 °C, priemer detskej, priemer výstupnej vody `tw2`, štarty kompresora.
Súhrn: priemer `ua` pre A a pre B, **úspora % = (ua_B − ua_A) / ua_B × 100**. Neistota: smerodajná odchýlka `ua` v rámci režimu
a orientačný interval ± 2 × sqrt(sd_A²/n_A + sd_B²/n_B) prepočítaný na percentá; ak je rozdiel menší ako tento interval, verdikt
„rozdiel je v rámci šumu, test nepreukázal úsporu“. Ďalej: rozdiel komfortu (hodiny mimo pásma, priemerná detská) a rozdiel
priemernej teploty vody. Upozorni, ak sa priemerný `t_out` dní A a B líši o > 3 °C. Výsledok ulož do `result`:
`{"days":[{date,mode,valid,note,hours,kwh,t_in,t_out,dT,ua,detska,mimo_pasma_h,tw2,starty}], "A":{n,ua,sd,kwh,t_in,t_out,detska,mimo_pasma_h,tw2},
"B":{...}, "uspora_pct", "neistota_pct", "verdikt": "2-4 vety po slovensky"}`. Priebežný výsledok počítaj aj počas testu (pre web).

## Vplyv na zvyšok systému počas testu (status `planned` alebo `running`)
- `check_action` odmietne všetky akcie s poznámkou „prebieha porovnávací test“ (agent nemení krivku ani nič iné).
- `build_input` pridá sekciu „POROVNÁVACÍ TEST“ (dnešný režim, deň X z N, že regulácia môže byť zámerne vypnutá) – model
  nemá hodnotiť vyššiu teplotu v B ako problém.
- `event_trigger`: izbové podmienky sa počas testu ignorujú (poplachy chyby/dohrevu ostávajú).
- Plánovač TÚV sa počas testu nespúšťa (`can_start` → False, „prebieha porovnávací test“).
- `needs_llm`: počas dní B model nevolá kvôli „detská mimo cieľa“ a „izba pod minimom“.
- Štart testu nie je dovolený, ak: agent nemá čerstvé dáta, chybový kód ≠ 0, alebo existuje čakajúci zásah typu ventil
  (vtedy web povie, odkedy sa dá test spustiť = `due_ts` zásahu).

## API (prihlásenie a `X-Kurenie: 1` ako ostatné)
| Metóda a cesta | Telo | Odpoveď |
|---|---|---|
| GET `/api/abtest` | – | `{"state": stav alebo null, "today": {"date","mode"}|null, "can_start": bool, "blocked_reason": str|null, "result": priebežný alebo finálny výsledok|null, "esp_mode": "Vypnutá"|"Posun krivky"|...|null}` |
| POST `/api/abtest_start` | `{"days": 6|8|10|14}` | `{"ok":true,"state":...}` / 400 `{"error"}` |
| POST `/api/abtest_stop` | `{}` | `{"ok":true}` (vráti režim A) |

`/api/status` dostane pole `abtest: {"status","today_mode","day_index","days_total"}|null`.

## Web (`ui.html`, zachovať vizuálny jazyk)
Nový panel „Porovnávací test“ (pred „Posledné dni“):
- Bez testu: 3–4 vety, čo test robí (striedanie dní s reguláciou podľa izby a bez nej, poistky, agent počas testu nič nemení,
  v dňoch B môže byť v izbách o niečo teplejšie), výber dĺžky (6/8/10/14 dní, predvolene 10), tlačidlo „Spustiť test“ s potvrdením
  priamo v stránke. Ak `can_start` je false, tlačidlo neaktívne a dôvod.
- Počas testu: pás dní (kalendár) s písmenom A/B, dnešný deň zvýraznený, neplatné dni prečiarknuté s poznámkou; dnešný režim
  slovne („Dnes: B – regulácia podľa izby vypnutá“); priebežný výsledok; tlačidlo „Zastaviť test“ s potvrdením.
- Výsledok: veľké číslo úspory v % (alebo verdikt o šume), neistota, tabuľka A vs B (dni, ua, kWh/deň, vnútri, vonku, detská,
  hodiny mimo pásma, voda), tabuľka po dňoch, verdikt. Popis metriky jednou vetou.
- Na stránke Agent pri teste pilulka „Test A/B · deň X z N“.
