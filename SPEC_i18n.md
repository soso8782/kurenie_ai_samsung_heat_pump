# SPEC: jazyky webu – `locales/sk.json`, `locales/en.json` (verzia 2.2.0, 15. 11. 2026)

Cieľ: všetky texty webového rozhrania (ui.html, stats_ui.js) idú z prekladových súborov `locales/<jazyk>.json`.
Slovenčina = pôvodné texty (bez zmeny významu ani formy), angličtina = prirodzený preklad pre majiteľa tepelného čerpadla.
Pridať ďalší jazyk = pridať `locales/xx.json` (bez zmeny kódu, okrem zoznamu v prepínači ktorý sa berie z `_meta`).
Len štandardná knižnica / čistý JS bez knižníc. Nič nenasadzuj, žiadne ssh/scp/git.

Mimo rozsahu (ostáva po slovensky): texty z backendu (chybové hlášky API, hodnotenia a správy AI agenta, upozornenia),
názvy entít z ESP a izieb z profilu, obsah `SPEC*.md`/docs.

## Formát súborov
```json
{
  "_meta": {"jazyk": "sk", "nazov": "Slovenčina", "locale": "sk-SK", "desatinna": ","},
  "nav.prehlad": "Prehľad",
  "prehlad.vonku": "Vonku",
  "spotreba.dni": "{n} dní",
  "stats.titul": "Štatistiky"
}
```
- Plochý objekt, kľúče `oblasť.názov` (malé písmená, a–z, 0–9, `_`, bodky), zoradené abecedne (okrem `_meta` prvé).
  Prefixy: `nav.`, `top.`, `prehlad.`, `spotreba.`, `izby.`, `vonku.`, `tuv.`, `den.`, `cidla.`, `analyzy.`, `nastavenia.`,
  `zariadenie.`, `agent.`, `login.`, `spolocne.` (opakované: Uložiť, Zrušiť, –, jednotky…), `cas.` (dni v týždni, mesiace),
  `stats.` (všetko zo stats_ui.js).
- Premenné `{nazov}` v texte; množné čísla: kľúče s príponou `.1`, `.2_4`, `.5` (slovenčina) a `.1`, `.n` (angličtina) +
  pomocná funkcia `tn(key, n, vars)` (viď nižšie). HTML v prekladoch nepoužívaj (okrem `<b>` kde už v pôvodnom texte je –
  vtedy kľúč končí `_html` a vkladá sa cez innerHTML; premenné sa vtedy escapujú).
- `sk.json` a `en.json` majú **presne rovnakú množinu kľúčov** (test to overí).

## Zmluva v ui.html (Úloha A ju vytvorí, Úloha B ju len používa)
Globálne (na `window`), definované na začiatku hlavného skriptu ui.html **pred** akýmkoľvek vykreslením:
- `t(key, vars)` → preložený text; chýbajúci kľúč → slovenský text, inak samotný kľúč (a `console.warn` raz na kľúč).
- `tn(key, n, vars)` → množné číslo (sk: n==1 → `.1`, 2–4 → `.2_4`, inak `.5`; en: n==1 → `.1`, inak `.n`); `vars.n` = n.
- `LANG` (napr. "sk"), `LOCALE` ("sk-SK"/"en-GB"), `fnum(v, n)` = číslo s desatinným oddeľovačom jazyka a „−“ pre mínus
  (existujúca funkcia `f()` v ui.html ju má používať).
- `i18nReady` = Promise, ktorý sa vyrieši po načítaní `/locales/<LANG>.json` (+ `/locales/sk.json` ako záloha).
  Štart aplikácie (prvé `fetch` statusu, vykreslenie, načítanie stats_ui.js) počká na `i18nReady`.
- `applyI18n(root)` – preloží statické HTML: `data-i18n="key"` (textContent), `data-i18n-html="key"` (innerHTML),
  `data-i18n-attr="placeholder:key;title:key;aria-label:key"`.
- Výber jazyka: `localStorage["kurenie-lang"]` (try/catch), inak `navigator.language` začína `sk`/`cs` → `sk`, inak `en`.
  `<html lang>` sa nastaví. Prepínač jazyka (SK / EN – zoznam z `_meta.nazov` známych jazykov, zatiaľ natvrdo ["sk","en"])
  v hornej lište vedľa prepínača témy (svetlý/systém/tmavý), rovnaký štýl segmentu; zmena = uloženie + `location.reload()`.
- Dátumy/časy: kde ui.html formátuje dátum ručne („15. 11.“, „po“, „november“), použi kľúče `cas.*` alebo `Intl.DateTimeFormat(LOCALE, …)`;
  v angličtine napr. „15 Nov“, „Mon“. Slovenský výstup musí ostať presne ako doteraz.

## Úloha A – infraštruktúra + ui.html (+ app.py, sw.js)
Súbory: `ui.html`, `app.py`, `sw.js`, `locales/sk.json`, `locales/en.json` (kľúče okrem `stats.*`), nový `test_i18n.py`.
- `app.py`: GET `/locales/<xx>.json` (regex `^/locales/[a-z]{2}\.json$`, súbor z `ROOT/locales`, bez prihlásenia ako ui.html,
  `application/json; charset=utf-8`, `Cache-Control: no-cache`); neexistujúci → 404. `MANIFEST.lang` ostáva "sk".
- `ui.html`: zmluva vyššie + **všetky** slovenské texty (HTML aj JS reťazce: tlačidlá, nadpisy, popisky, placeholdery, title,
  aria-label, hlášky, tooltipy, prázdne stavy, potvrdenia, texty generované v JS) nahraď kľúčmi. Názvy izieb/entít z API neprekladaj.
  Pozor na sprievodcu „Zariadenie a dom“, Spotrebu, „Ako kúri podľa vonku“, A/B test, zásahy, Čidlá, Nastavenia, prihlásenie.
- `sw.js`: zvýš `SW_VERSION`.
- `test_i18n.py`: rovnaké kľúče v sk/en, platný JSON, `_meta`; žiadny kľúč použitý v ui.html/stats_ui.js
  (regex na `t("…")`, `tn("…"`, `data-i18n="…"`, `data-i18n-attr`) nechýba v sk.json; route `/locales/sk.json` vráti 200 a `/locales/../x` 404;
  heuristika: v ui.html v `<script>` a v texte HTML neostal reťazec s diakritikou SK (á ä č ď é í ľ ĺ ň ó ô ŕ š ť ú ý ž)
  mimo komentárov a mimo zoznamu výnimiek (zdôvodni každú výnimku).

## Úloha B – stats_ui.js
Súbory: `stats_ui.js`, `stats_ui.css` (ak treba), `locales/stats.sk.json`, `locales/stats.en.json` (len kľúče `stats.*` –
hlavný agent ich potom zlúči do sk.json/en.json; nevytváraj ani nemeň `locales/sk.json`/`en.json`).
- Všetky slovenské texty v stats_ui.js nahraď `t("stats.…")` / `tn(...)`; formát čísel cez `fnum`, dátumy cez `LOCALE`.
- stats_ui.js sa načíta až po `i18nReady` (zabezpečí A) – môžeš predpokladať, že `t`, `tn`, `fnum`, `LANG`, `LOCALE` existujú;
  pre istotu na začiatku súboru fallback: ak `window.t` neexistuje, `t = k => k` (aby sa stránka nezrútila).
- Názvy metrík a skupín, ktoré prichádzajú z API (`/api/stats/meta`, generuje ich stats.py po slovensky), ponechaj – nie sú
  v rozsahu (len ak je v stats_ui.js lokálny záložný zoznam `DEFAULT_METRICS`, prelož jeho názvy).

## Odovzdanie (obe úlohy)
Zmenené súbory, počet kľúčov, výstup testov (`python -X utf8 -m unittest test_i18n test_v2_web` – B len svoje kontroly),
overenie syntaxe JS (Edge headless + new Function, ako v predošlých úlohách), zoznam textov, ktoré ostali po slovensky a prečo.
Python: "C:\Program Files\KiCad\10.0\bin\python.exe" -X utf8, z priečinka gh_kurenie.
