# Kúrenie AI pre Home Assistant

[![English](https://img.shields.io/badge/lang-English-blue.svg)](README.md) [![Slovenčina](https://img.shields.io/badge/jazyk-Sloven%C4%8Dina-green.svg)](README.sk.md)

> **Integrácia sa udržiava v samostatnom repozitári:**
> **[samsung_heat_pump-ai-control-home-assistant-integration](https://github.com/soso8782/samsung_heat_pump-ai-control-home-assistant-integration)**.
> Do HACS pridaj **jeho** adresu (nie adresu tohto repozitára). Kópia v tomto priečinku môže byť staršia.

Integrácia pripojí Home Assistant na web aplikáciu **Kúrenie AI** (agent pre tepelné čerpadlo Samsung) a vytvorí jedno zariadenie
s entitami na sledovanie aj ovládanie. Dáta sa čítajú lokálne (`local_polling`), predvolene každých 30 s.

Spotrebu a COP pre panel Energia integrácia neprináša: tie ostávajú na MQTT senzoroch z `ha_pub.py`.

## Inštalácia

**Cez HACS (custom repository):**
1. HACS, menu vpravo hore, Vlastné repozitáre, vlož adresu repozitára `https://github.com/soso8782/samsung_heat_pump-ai-control-home-assistant-integration`, kategória Integrácia.
2. Nainštaluj „Kúrenie AI“ a reštartuj Home Assistant.

**Ručne:** skopíruj priečinok `custom_components/kurenie_ai` do `config/custom_components/` a reštartuj Home Assistant.

## Nastavenie

1. V aplikácii Kúrenie AI otvor Nastavenia, karta **Integrácia Home Assistant**, a daj **Vytvoriť kľúč**.
   Kľúč sa zobrazí iba raz, skopíruj si ho. Starý kľúč po vytvorení nového prestane platiť, **Zrušiť kľúč** integráciu odpojí.
2. V Home Assistante: Nastavenia, Zariadenia a služby, Pridať integráciu, **Kúrenie AI**.
3. Zadaj adresu aplikácie (napr. `http://10.10.40.30`) a kľúč.
4. Interval obnovy (15 až 300 s) zmeníš cez tlačidlo Konfigurovať pri integrácii.
   Ak kľúč zrušíš alebo vymeníš, Home Assistant ponúkne opätovné prihlásenie (reauth).

## Entity

Zariadenie „Kúrenie AI – <názov zariadenia>“ (výrobca Samsung).

| Typ | Entita |
|---|---|
| sensor | Vonkajšia teplota; teplota každej izby (atribúty `min_target`, `reference`); výstupná teplota vody, spiatočka, cieľová teplota podľa krivky, Delta T; frekvencia kompresora; posun krivky; teplota TÚV (ak je TÚV); chybový kód; stav agenta (ok / pozor / problem); posledné hodnotenie agenta (celý text v atribúte `summary`); poplachy (počet, zoznam v atribúte `alerts`) |
| binary_sensor | Kompresor beží; odmrazovanie; ohrev TÚV (ak je TÚV); problém (poplach alebo chybový kód ≠ 0); pripojenie ESP, zberača a Home Assistantu |
| select | Režim agenta (vypnutý / radí / riadi) |
| number | Cieľová izbová teplota (krok 0,5, rozsah podľa aplikácie) |
| button | Spustiť agenta teraz; spustiť ohrev TÚV (ak je TÚV) |

Po každej akcii sa dáta obnovia okamžite. Ak aplikácia akciu odmietne (napr. ohrev TÚV teraz nejde), Home Assistant zobrazí jej správu.

## Riešenie problémov

- „Nepodarilo sa pripojiť“: skontroluj adresu a že aplikácia beží; integrácia používa adresu z aplikácie priamo (http alebo https).
- „Kľúč je neplatný“: vytvor v aplikácii nový kľúč a zadaj ho znova.
- Podrobné záznamy: `logger: logs: custom_components.kurenie_ai: debug`.
