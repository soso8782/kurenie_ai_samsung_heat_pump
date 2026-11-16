# Verzia 2.4.0 – úplná angličtina pre anglicky hovoriacich používateľov

Cieľ: používateľ s `UI_LANG=en` (config.env) a webom prepnutým na EN nesmie nikde stretnúť slovenský text, ktorý vytvorila
aplikácia. Výnimky (NEPREKLADAŤ): obsah DB z minulosti (staré hodnotenia, správy, poučenia), čo si používateľ sám napísal
alebo pomenoval (názvy izieb, poznámky, správy agentovi, názvy entít z jeho profilu/ESP), `SPEC*.md`, komentáre v kóde,
`print`/logy pre správcu a CLI výpisy (`stats.py usage` a pod.), SQL a interné značky (napr. `reason!='vrátené používateľom'`
– to sú hodnoty v DB, nemeniť!).

Existujúci mechanizmus (verzia 2.3.0, pozri `SPEC_server_i18n.md`, `i18n.py`): `tr(key, lang, **kw)`, `tn(...)`,
`req_lang(handler)` (hlavička X-Lang z webu), `home_lang(cfg)` (UI_LANG – jazyk upozornení a agenta). Kľúče `srv.*` v
`locales/sk.json` a `locales/en.json` (ploché kľúče, `en` musí mať všetky kľúče ako `sk`). Text v `sk` musí ostať presne
taký, aký je dnes (byte po byte).

## Časť A – texty pre používateľa, ktoré ešte nie sú preložené

Nájdi ich auditom (skript nižšie) + kontrolou za behu. Kandidáti zistení auditom (overiť, či už nejdú cez `tr`, a doplniť):
- `ha_ws.py` – chybové hlášky spojenia s HA (zobrazujú sa na webe v stave pripojenia).
- `stats.py` – katalóg porúch (L~117–131: názov, popis, rada), texty porúch „Výpadok dát trvá už…“, „Chybový kód tepelného
  čerpadla E%d.“ (POZOR: L~1013 regex číta tieto texty späť z DB – riešenie musí fungovať aj pre staré slovenské záznamy;
  najlepšie ukladať do DB štruktúrovane / slovensky ako doteraz a prekladať pri zobrazení), popisy kalibrácie (L~1334–1380),
  názvy porovnaní „Včera / Pred týždňom / Pred 2 týždňami“, „TÚV / dosiahnutý cieľ / iné“, zdroj COP „tabuľky výrobcu…“.
- `collector.py` – texty udalostí („ESP znova dostupné“, „ESP nedostupné“, „Zberač spustený“, „Nenulový chybový kód TČ“…):
  ukladaj v jazyku `home_lang` (alebo prekladaj pri zobrazení, ak je to jednoduché a spoľahlivé).
- `notify.py`, `app.py` (napr. „Na túto akciu nemáš oprávnenie.“, „Modul štatistík nie je nainštalovaný.“, „správca“,
  „neplatný čas“), `dhw.py`, `abtest.py` (stavy a dôvody viditeľné na webe), `contacts.py`/`temps.py` (skupiny „Ostatné“,
  „Hlavica radiátora“, „nenastavené“…), `ha_api.py` (chybové správy akcií – jazyk podľa `home_lang`), `ha_pub.py`,
  `profil.py` (popisné názvy nastavení FSV a PRESETS, ak sa zobrazujú na webe), `interventions.py`, `stats_backfill.py`,
  `vonku.py`, `t_energia.py`, `cfjwt.py`, `cftunnel.py`, `contacts_mqtt.py`.
- `ui.html`, `stats_ui.js`, `sw.js` – skontroluj zvyšné slovenské reťazce v JS/HTML mimo `t()`/`data-i18n`.

Rozhodnutie pre každý reťazec: zobrazí sa používateľovi (web, upozornenie, HA, CSV export hlavičky) → preložiť;
log/print/SQL/regex nad DB/komentár → nechať.

## Časť B – pokyny pre AI agenta v angličtine

Teraz: prompty v `agent.py` (SYSTEM, LEARN_SYSTEM, CHAT_SYSTEM, CHAT_SYSTEM_USER, a všetko, čo skladá vstup modelu – popisy
dát, nadpisy sekcií, rutinné texty), bloky pre model v `contacts.py` (OKNÁ A DVERE…), `temps.py` (TEPLOTNÉ ČIDLÁ…), `abtest.py`
(L~637–643 kontext A/B testu) a ďalšie, ktoré do vstupu modelu vkladajú text, sú po slovensky + na konci pokyn jazyka.

Úloha: pri `home_lang == "en"` posielaj modelu **celý vstup po anglicky** (systémové prompty aj zostavený vstup).
Pri `sk` musí byť vstup **byte po byte rovnaký ako dnes** (vrátane pokynu jazyka) – over testom: pred úpravou si ulož
snapshot zostaveného SK promptu/vstupu pre pevné testovacie dáta (alebo ich vygeneruj z kópie DB, ak ju máš), po úprave porovnaj.
- Anglický preklad musí zachovať význam, všetky čísla, mantinely, kľúče JSON, názvy polí a formát výstupu (JSON schéma
  odpovede sa nemení – kľúče ostávajú rovnaké, inak by sa rozbilo parsovanie).
- Ak model vracia hodnoty enumov (napr. stav „ok/pozor/problem“), tie ostávajú rovnaké v oboch jazykoch.
- Prompty môžu byť v `agent.py` ako dvojice SK/EN konštánt (prehľadnejšie než kľúče v locales); krátke opakujúce sa časti
  vstupu cez `tr` s kľúčmi `srv.agent.*` / `srv.prompt.*`.

## Testy a overenie

- Nový `test_en_kompletne.py`: (1) audit – v `en` režime nie je v textoch pre používateľa slovenská diakritika (kde sa dá
  testovať priamo volaním funkcií s lang="en"); (2) SK prompt snapshot sa nezmenil; (3) EN prompt neobsahuje slovenskú
  diakritiku okrem dát (názvy izieb z profilu – testuj s profilom s anglickými názvami); (4) `en.json` má všetky kľúče `sk.json`.
- Všetky existujúce testy musia prejsť: `python -X utf8 -m unittest test_server_i18n test_i18n test_ha_api test_v2_web
  test_v2_jadro test_v2_stats test_cop test_vonku test_spotreba test_mqtt` + `python -X utf8 -m unittest discover -s tests_ha`.
- `VERSION` = 2.4.0, záznam do `CHANGELOG.md` (po slovensky ako doterajšie).

Audit skript: `python -X utf8 <scratchpad>\lead_audit.py . súbor1.py súbor2.py` vypíše slovenské reťazce (bez docstringov).
