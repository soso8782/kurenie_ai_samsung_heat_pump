# Zmeny

Formát podľa [Keep a Changelog](https://keepachangelog.com/sk/1.1.0/), čísla verzií podľa [Semantic Versioning](https://semver.org/).

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

[1.0.0]: https://github.com/soso8782/kurenie_ai_samsung_heat_pump/releases/tag/v1.0.0
