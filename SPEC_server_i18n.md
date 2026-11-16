# SPEC: preklad textov zo servera (verzia 2.3.0, 16. 11. 2026)

Nadväzuje na SPEC_i18n.md (web už je v `locales/sk.json` / `en.json`). Teraz sa prekladá aj to, čo posiela server.
Len štandardná knižnica. Nič nenasadzuj, žiadne ssh/scp/git. Slovenský výstup musí ostať **identický** s doterajším.

## Jazyk
- Jazyk požiadavky z webu: hlavička `X-Lang` (ui.html ju pridá ku každému `fetch` na `/api/` – hodnota `LANG`), inak `sk`.
- Jazyk „domácnosti“ pre texty bez požiadavky (upozornenia na telefón, hodnotenia a správy AI agenta, týždenný súhrn,
  HA senzory/MQTT): nové nastavenie `UI_LANG` v `config.env` (`sk` predvolené, `en`). Na webe: Nastavenia → karta Agent alebo
  všeobecné – výber „Jazyk upozornení a agenta“ (POST `/api/settings` {ui_lang}). Prepínač SK/EN v hornej lište ho nemení.

## Modul `i18n.py` (nový)
- `tr(key, lang="sk", **vars)` – text z `locales/<lang>.json`, kľúče s prefixom `srv.`; chýbajúci → sk → kľúč. `{n}` atď. ako na webe.
- `tn(key, n, lang, **vars)` – množné čísla rovnako ako na webe (`.1`, `.2_4`, `.5`, `.n`).
- `req_lang(headers)` – z `X-Lang` (len známe jazyky z priečinka `locales/`), inak `sk`.
- `home_lang(cfg)` – `UI_LANG` z cfg, inak `sk`.
- Súbory sa načítajú raz a znova pri zmene mtime.
- Kľúče servera patria do **tých istých** `locales/sk.json` a `locales/en.json` (prefix `srv.`, abecedne, rovnaká množina v oboch).

## Čo preložiť
1. **Chybové a informačné hlášky API** (`{"error": "…"}`, `{"dovod": …}`, texty v odpovediach, ktoré web zobrazuje) v `app.py`,
   `spotreba.py`, `vonku.py`, `stats.py` (API štatistík), `contacts.py`, `temps.py`, `ha_auth.py`, `dhw.py`, `interventions.py`,
   `abtest.py`, `profil.py` (chyby validácie), `cftunnel.py`, `notify.py` (výsledky testov). Funkcie, ktoré text vracajú, dostanú
   parameter `lang="sk"`; `app.py` ho odovzdá z `i18n.req_lang(self.headers)`.
2. **Názvy metrík štatistík** (`stats.py` `_m(...)` zoznam, skupiny, rýchle voľby „Včera“…, dôvody cyklov) – v API podľa jazyka požiadavky.
3. **Upozornenia** (`notify.py` a všetky volania `notify.event(...)`) – v jazyku `home_lang(cfg)`.
4. **AI agent** (`agent.py`): do systémových promptov (SYSTEM, LEARN_SYSTEM, CHAT_SYSTEM, CHAT_SYSTEM_USER) pridaj na koniec pokyn
   „Odpovedaj v jazyku: <slovenčina|English>“ podľa `home_lang`. **Samotné prompty ostávajú po slovensky** (overené správanie
   agenta – nemeniť). Texty, ktoré agent skladá sám bez modelu (rutinné zhrnutia, dôvody, `log_rutina`, týždenný súhrn), prelož cez `tr`.
5. **HA senzory cez MQTT** (`ha_pub.py`): názvy entít v discovery podľa `home_lang` (unique_id sa NESMIE meniť).

Mimo rozsahu: obsah DB z minulosti (staré hodnotenia ostanú v jazyku, v akom vznikli), názvy z profilu (izby, entity ESP),
`SPEC*.md`, komentáre a logy pre správcu (`print`), docs.

## ui.html
- `api()` / fetch: pridaj hlavičku `X-Lang: LANG` ku všetkým požiadavkám na `/api/`.
- Nastavenia: výber `UI_LANG` (kľúče `nastavenia.jazyk_domacnosti*` v sk/en.json).

## Testy – `test_server_i18n.py`
`tr`/`tn` (fallback, premenné, množné čísla), `req_lang`, rovnaké kľúče `srv.*` v sk/en, každý `tr("srv.…")` v *.py existuje,
vybrané API odpovede v `en` nemajú slovenskú diakritiku (napr. `/api/spotreba` s neplatným dátumom, `/api/profil` POST s chybou),
v `sk` sú identické s doterajšími textami, prompt agenta obsahuje pokyn jazyka. Všetky existujúce testy musia prejsť
(`test_i18n` – uprav len ak je to nutné kvôli novým `srv.*` kľúčom a zdôvodni).

## Odovzdanie
Zmenené súbory, počet `srv.*` kľúčov, výstup všetkých testov (`python -X utf8 -m unittest test_server_i18n test_i18n test_v2_web
test_v2_jadro test_v2_stats test_cop test_vonku test_spotreba test_mqtt`), čo ostalo po slovensky a prečo.
Python: "C:\Program Files\KiCad\10.0\bin\python.exe" -X utf8 z priečinka gh_kurenie.
