# Zmeny

Formát podľa [Keep a Changelog](https://keepachangelog.com/sk/1.1.0/), čísla verzií podľa [Semantic Versioning](https://semver.org/).

## [2.1.1] – 2026-11-15

### Pridané
- Prehľad: riadok s typom tepelného čerpadla z profilu (názov, Split/Mono, výkon, fázy, chladivo, typ kúrenia, TÚV, model
  a či sa COP počíta z overenej tabuľky výrobcu); správca sa z neho preklikne do Nastavení. `/api/status` → `profil_info.zariadenie_info`.

## [2.1.0] – 2026-11-15

### Pridané
- **25 nových modelov** v `modely/` z dátových kníh výrobcu (tabuľky Maximum Heating Capacity, Peak): Mono R32
  `AE050/080/120/160RXYD*G`, Mono R290 `AE050/080/120/160CXYD*K` a `…CXYB*K` (so zabudovaným čerpadlom), Split R32
  `AE040/060/090RXED*G`. Spolu 31 modelov; overený na reálnom zariadení zostáva len AE120JXEDGH.
- `docs/MODELY.md` – zoznam modelov so zdrojmi a návod, ako pridať ďalší.

### Opravené
- Hodinové štatistiky: chyba formátovania SQL pri načítaní ďalších teplôt (chodba, kotolňa) z `agent.db`.
- Test predvoleného profilu sa preskočí, ak má inštalácia vlastný `profil.json`.

## [2.0.0] – 2026-11-15

Aplikáciu možno nastaviť pre iné tepelné čerpadlo Samsung EHS a iný dom bez úprav kódu. Bez `profil.json` sa správa
presne ako 1.0.0.

### Pridané
- **Profil inštalácie** (`profil.json`, modul `profil.py`): model a typ zariadenia, typ kúrenia (radiátory / podlahové /
  kombinované), názvy entít v ESP, izby (kľúč, názov, ESP entita, HA senzory, minimum, referenčná izba), ďalšie teploty,
  zdroje histórie z HA, mantinely agenta s pevnými absolútnymi hranicami, elektromer. Podrobnosti: `docs/PROFIL.md`,
  vzor `profil.example.json`.
- **Sprievodca „Zariadenie a dom“** v Nastaveniach (admin): 4 kroky (zariadenie, entity ESP s automatickým návrhom,
  izby, mantinely), uloženie s chybami pri poliach, návrat na predvolené, reštart služieb z webu.
  Nové API `/api/profil`, `/api/profil/predvoleny`, `/api/profil/esp_entity`, `/api/profil/ha_entity`, `/api/restart`.
- **Knižnica modelov** `modely/*.json`: tabuľky Maximum Heating Capacity (Peak) pre Samsung EHS Split
  AE040/060/090/120/140/160JXED*H z dokumentácie výrobcu (overený na reálnom zariadení je len AE120JXEDGH).
- **ESPHome šablóna** `esphome/samsung-ehs.yaml` s názvami entít podľa predvoleného profilu a návod `esphome/README.md`.
- `profil_info` v `/api/status`: zariadenie, izby a dostupné funkcie.

### Zmenené
- Izby, referenčná izba, názvy entít, mantinely agenta, stĺpce hodinovej štatistiky a import histórie z HA sa berú z profilu
  (konštanty ostávajú pod pôvodnými menami); `hourly` v `stats.db` sa pri novej izbe automaticky rozšíri o stĺpec.
- Prompty agenta používajú izby a referenčnú izbu z profilu a vedia, či ide o radiátory alebo podlahovku.
- Tabuľka výrobcu pre COP a kalibráciu prietoku sa číta z `modely/`; bez tabuľky sa použije prietok zo senzora alebo
  z konfigurácie, inak sa výkon a COP nezobrazia (bez chyby).
- Webové rozhranie skrýva funkcie, ktoré nie sú k dispozícii (TÚV, spotreba domu, agent bez kľúča), namiesto chýb.
- Adresa ESP: `TC_ESP`, inak `ESP_URL` z `config.env`; elektromer: prednosť má pole `elektrina` v profile.

## [1.0.0] – 2026-11-15

Prvé verejné vydanie.

### Funkcie
- Zberač dát z ESPHome mosta na Samsung NASA zbernici (SSE + REST, minútové vzorky).
- Webová aplikácia / PWA: prehľad, izby, TÚV, deň po hodinách, štatistiky, čidlá, analýzy, nastavenia; tmavý/svetlý režim.
- **Ako kúri podľa vonku** – nastavenie ekvitermickej krivky (FSV), história zmien, skutočná teplota vody a izieb podľa vonkajšej teploty.
- **Spotreba** – denná spotreba domu a tepelného čerpadla s odhadom ceny podľa cenníka (DD5, energopomoc), mesačné súhrny.
- Presný výpočet COP (prietok kalibrovaný z tabuliek výrobcu, príkon kalibrovaný na nočnú spotrebu domu).
- Home Assistant: senzory cez MQTT discovery, externé štatistiky pre Energy panel, okenné kontakty a teplotné čidlá.
- AI agent cez OpenRouter (režimy Radí / Riadi) s pevnými mantinelmi, denné učenie, správy agentovi, týždenné súhrny, A/B test.
- Plánovač a manuálny ohrev TÚV, zásahy s vyhodnotením.
- Upozornenia cez Web Push a HA webhook; prihlásenie heslom, Cloudflare Tunnel + Access (JWT), role admin/user.

[2.1.1]: https://github.com/soso8782/kurenie_ai_samsung_heat_pump/releases/tag/v2.1.1
[2.1.0]: https://github.com/soso8782/kurenie_ai_samsung_heat_pump/releases/tag/v2.1.0
[2.0.0]: https://github.com/soso8782/kurenie_ai_samsung_heat_pump/releases/tag/v2.0.0
[1.0.0]: https://github.com/soso8782/kurenie_ai_samsung_heat_pump/releases/tag/v1.0.0
