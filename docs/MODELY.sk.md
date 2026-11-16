# Knižnica modelov (`modely/`)

[![English](https://img.shields.io/badge/lang-English-blue.svg)](MODELY.md) [![Slovenčina](https://img.shields.io/badge/jazyk-Sloven%C4%8Dina-green.svg)](MODELY.sk.md)

Každý súbor `modely/<ID>.json` je tabuľka výrobcu **Maximum Heating Capacity (Peak Value)** jednej vonkajšej jednotky:
vykurovací výkon (HC, kW) a príkon (PI, kW) podľa vonkajšej teploty a teploty výstupnej vody. Aplikácia z nej počíta
tabuľkový COP a kalibruje prietok okruhov.

## Modely v knižnici

| Rad | Modely | Rozsah tabuľky | Zdroj |
|---|---|---|---|
| Split R410A (gen. J) | AE040JXEDEH, AE060JXEDEH, AE090JXEDGH, **AE120JXEDGH** (overený), AE140JXEDGH, AE160JXEDGH | −20…20 °C vonku, voda 25–55 °C | EHS Split Technical Data Book (Europe) |
| Split R32 | AE040RXEDEG, AE060RXEDEG, AE090RXEDEG/GG | −20…20 °C, voda 25–55 °C | EHS Split Technical Data Book (Europe) |
| Mono R32 | AE050RXYDEG, AE080RXYDEG/GG, AE120RXYDEG/GG, AE160RXYDEG/GG | −20…20 °C, voda 30–65 °C | EHS tank integrated Hydro Unit TDB (Mono, R32) |
| Mono R290 | AE050CXYDEK, AE080CXYDEK/GK, AE120CXYDEK/GK, AE160CXYDEK/GK | −25…35 °C, voda 25–75 °C | EHS Mono R290 TDB |
| Mono R290 so zabudovaným čerpadlom | AE050CXYBEK, AE080CXYBEK/GK, AE120CXYBEK/GK, AE160CXYBEK/GK | −25…35 °C, voda 25–75 °C | EHS Mono R290 Pump TDB |

Presný názov dokumentu a URL je v poli `zdroj` každého súboru. Čísla boli z PDF vyčítané skriptom (nie prepisované ručne)
a namátkovo porovnané s textom dokumentu. **Na reálnom zariadení je overený iba AE120JXEDGH** – ostatné majú
`"overene": false`. Kde výrobca uvádza jednu tabuľku pre 1-fázovú aj 3-fázovú verziu (`AE080RXYD*G`), sú v knižnici dva
súbory s rovnakými hodnotami (`…EG` a `…GG`).

Chýba: Mono HT Quiet (`AE…BXYD…`) – verejne dostupná dátová kniha s čitateľnou tabuľkou sa nenašla.

## Ako pridať model

1. V Samsung **Technical Data Book** k vonkajšej jednotke nájdi kapitolu *Capacity table → Maximum Heating Capacity (Peak Value)*.
2. Vytvor `modely/<OZNAČENIE>.json` (názov súboru = ID, len písmená, číslice a `- _ .`):

```json
{
  "nazov": "Samsung EHS Mono R32 8 kW AE080RXYDEG (1-fáz.)",
  "typ": "mono",
  "vykon_kw": 8,
  "chladivo": "R32",
  "zdroj": "názov dokumentu, verzia, kapitola, URL",
  "overene": false,
  "ta": [-20, -15, -10, -7, -2, 2, 7, 10, 15, 20],
  "lw": [30, 35, 40, 45, 50, 55, 60, 65],
  "peak": {
    "-20": [[5.60, 2.32], [5.33, 2.57], [5.27, 2.74], [5.20, 3.13], null, null, null, null],
    "-15": [[6.56, 2.54], [6.25, 2.82], [6.15, 2.98], [6.06, 3.16], [5.88, 3.32], null, null, null]
  }
}
```

   - `ta` = vonkajšie teploty (riadky), `lw` = teploty výstupnej vody (stĺpce), `peak["<ta>"]` = bunky `[HC kW, PI kW]`
     v poradí `lw`; riadok musí mať presne toľko buniek ako `lw`.
   - Bunka, ktorú výrobca neuvádza, je `null` (prvá bunka riadku `null` byť nesmie).
   - Ak je tabuľka vo wattoch, prepočítaj na kW. `typ`: `split` / `mono` / `hydrosplit`.
3. Over: `python3 -c "import profil; print([m['id'] for m in profil.list_models()])"` – model musí byť v zozname.
4. Vo webe **Nastavenia → Zariadenie a dom** vyber model, ulož a reštartuj služby.
5. `"overene": true` nastav až po porovnaní vypočítaného COP so skutočným meraním (elektromer, kalorimeter).
