# Kúrenie AI – inteligentné riadenie tepelného čerpadla Samsung EHS

Domáci systém, ktorý zbiera dáta z tepelného čerpadla **Samsung EHS** (cez NASA zbernicu a ESPHome), počíta skutočnú spotrebu
a COP, sleduje teploty v izbách a pomocou jazykového modelu (cez [OpenRouter](https://openrouter.ai)) **radí alebo priamo
nastavuje ekvitermickú krivku** v bezpečných mantineloch. Všetko beží na malom Linux kontajneri (LXC), iba so štandardnou
knižnicou Pythonu 3 – žiadne pip balíčky.

> *English summary:* Self-hosted monitoring and AI-assisted control for a Samsung EHS air-to-water heat pump. A collector reads
> the unit via an ESPHome bridge on the Samsung NASA bus, a pure-stdlib Python web app shows rooms, hot water, energy, COP and
> costs, and an LLM agent (via OpenRouter) adjusts the weather-compensation curve within hard safety limits. UI is in Slovak.

## Čo to vie

- **Prehľad v reálnom čase** – teplota vody (výstup/spiatočka, ΔT), kompresor, odmrazovanie, 3-cestný ventil, TÚV, chybové kódy.
- **Izby** – teploty z Home Assistant (Zigbee/ESPHome čidlá), cieľové minimá, detail izby s grafom, otvorené okná/dvere.
- **Ako kúri podľa vonku** – nastavenie krivky (FSV 2011/2012/2021/2022/2031/2032/4013, posun, zosilnenie), história zmien FSV,
  graf a tabuľka skutočnej teploty vody pri danej vonkajšej teplote (len minúty, keď naozaj kúri do radiátorov) a min/max
  teploty izieb podľa vonkajšej teploty.
- **Spotreba a cena** – denná spotreba celého domu (z elektromera / portálu distribúcie) a tepelného čerpadla, odhad ceny podľa
  cenníka (DD5, VT/NT, mesačná platba, adresná energopomoc), mesačné súhrny, fakturačné obdobie.
- **Presný výpočet COP** – tepelný výkon z prietoku a ΔT (prietok kalibrovaný podľa tabuliek výrobcu), príkon kalibrovaný
  na nočnú spotrebu domu; denné/týždenné/mesačné/ročné COP.
- **Home Assistant integrácia** – senzory cez MQTT discovery (príkon, spotreba, teplo, COP, obdobia), externé štatistiky
  do Energy panelu (spotreba domu, náklady, čerpadlo).
- **AI agent** – každú hodinu (alebo pri udalosti) vyhodnotí stav, predpoveď počasia a históriu; v režime *Radí* len odporúča,
  v režime *Riadi* mení povolené nastavenia. Denné „učenie“ z predošlých dní, správy agentovi, týždenné súhrny, A/B test.
- **TÚV** – plánovač a manuálny ohrev teplej vody.
- **Upozornenia** – Web Push (PWA na iPhone/Android) a HA webhook: chyba TČ, dohrev, okno otvorené pri kúrení, kredit, …
- **Prihlásenie** – heslo pre správcu, voliteľne Cloudflare Tunnel + Access (overenie JWT), role admin/user.
- **Štatistiky** – hodinová databáza, cykly kompresora, poruchy, porovnania, export.

## Architektúra

```
Samsung EHS ──NASA bus──> ESPHome (M5Stack Atom, m5stack-tc) ──REST/SSE──> collector.py ──> tc.db (minútové vzorky)
                                                                                   │
Home Assistant (izby, kontakty, elektromer) <──WS/REST/MQTT──> agent.py / stats.py ─┴─> stats.db (hodinové), agent.db
                                                                   │
                                          OpenRouter (LLM) <───────┤
                                                                   └──> app.py + ui.html  (web/PWA, port 80)
```

| Súbor | Účel |
|---|---|
| `collector.py` | zberač z ESP (SSE + REST, každú minútu) do `tc.db` |
| `agent.py` | hodinový beh agenta, mantinely, učenie, upozornenia, MQTT publikovanie |
| `app.py`, `ui.html`, `sw.js` | webový server (http.server) a jednostránková aplikácia / PWA |
| `stats.py`, `stats_ui.js/css`, `stats_backfill.py` | hodinové štatistiky, COP, kalibrácia, import histórie z HA |
| `spotreba.py`, `ha_cost.py` | denná spotreba a cena, cenníky, import do HA štatistík |
| `vonku.py` | „Ako kúri podľa vonku“ – krivka, voda a izby podľa vonkajšej teploty |
| `ha_pub.py` | senzory pre Home Assistant cez MQTT discovery |
| `ha_ws.py` | minimálny WebSocket klient pre Home Assistant |
| `contacts.py`, `contacts_mqtt.py`, `temps.py` | okenné/dverné kontakty a teplotné čidlá |
| `dhw.py` | teplá úžitková voda (plánovač, manuálny ohrev) |
| `interventions.py`, `abtest.py` | zásahy (ventily…) s vyhodnotením, A/B test nastavení |
| `notify.py`, `webpush.py` | upozornenia (HA webhook, Web Push s VAPID) |
| `cftunnel.py`, `cfjwt.py` | Cloudflare Tunnel a overovanie Cloudflare Access JWT |
| `monitor/` | pôvodný jednoduchý monitor (port 8099) |
| `SPEC*.md` | návrhy jednotlivých funkcií |
| `test_*.py`, `t_*.py` | testy (unittest) |

## Požiadavky

- Tepelné čerpadlo Samsung EHS (testované: Split gen. J, AE120JXEDGH + hydrobox AE160JNYDGH, 2018, R410A).
- ESPHome most na NASA zbernici, napr. M5Stack Atom s komponentom
  [`Beormund/esphome-samsung-nasa`](https://github.com/Beormund/esphome-samsung-nasa) a `web_server` (REST + SSE).
  Názvy entít, ktoré kód očakáva, sú v `common.py` (`S`) a `stats.py` (`TC`).
- Linux (Debian 12/13 LXC stačí), Python 3.11+, systemd. Žiadne ďalšie balíčky.
- Voliteľne: Home Assistant (izby, kontakty, elektromer, Energy panel), MQTT broker, OpenRouter kľúč, Cloudflare účet.

## Inštalácia

```bash
git clone https://github.com/soso8782/kurenie_ai_samsung_heat_pump.git /opt/kurenie
cd /opt/kurenie
cp config.env.example config.env && chmod 600 config.env
nano config.env          # ESP_URL, LAT/LON, prípadne HA_URL, NOTIFY_URL …
sh install.sh            # vytvorí a spustí systemd služby kurenie-collector/-web/-agent/-monitor
```

Potom otvor `http://<ip-kontajnera>/`, nastav heslo správcu a v **Nastaveniach** vlož kľúč OpenRouter, token Home Assistant,
MQTT a pod. Tieto tajomstvá sa ukladajú len lokálne do `config.env` (chmod 600) a späť sa nezobrazujú.

Denná záloha databáz: `systemd/kurenie-backup.service` + `.timer` (spúšťa `backup.sh`). Vzorové jednotky sú v `systemd/`.

Testy:

```bash
python3 -m unittest test_cop test_spotreba test_vonku test_mqtt
```

## Bezpečnosť a mantinely

- Agent smie meniť len nastavenia zo zoznamu `ALLOWED` v `agent.py` (napr. FSV 2031 42–50 °C, FSV 2032 32–38 °C, FSV 4013,
  maximálny kladný posun krivky) a len o malý krok za 12–24 h, nanajvýš 2 zmeny na beh, nikdy pri chybe TČ alebo starých dátach.
- `DRY_RUN=1` – nič sa neodošle do ESP ani na webhooky (len log).
- V repozitári nie sú žiadne kľúče, tokeny ani databázy (`.gitignore`). `config.env`, `cf_token`, `vapid.json`, `*.db`
  a história spotreby zostávajú len na serveri.

## Prispôsobenie

Kód vznikol pre konkrétny dom (izby, entity v HA, cenník SSE/SSD na Slovensku). Pri inej inštalácii uprav najmä:
`ROOMS`/`ROOM_NAMES` a mapovanie entít v `common.py` a `stats.py`, ID elektromera (`ENERGY_EXT_ID`, `ENERGY_METER_IDS`
v `ha_ws.py` – dajú sa nastaviť premennými prostredia) a cenníky `TARIFF` v `ha_cost.py`.

## Upozornenie

Projekt je hobby, bez záruky. Zmeny nastavení tepelného čerpadla robíš na vlastnú zodpovednosť – začni v režime *Radí*
alebo s `DRY_RUN=1`.
