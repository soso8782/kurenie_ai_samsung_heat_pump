# Model library (`modely/`)

[![English](https://img.shields.io/badge/lang-English-blue.svg)](MODELY.md) [![Slovenčina](https://img.shields.io/badge/jazyk-Sloven%C4%8Dina-green.svg)](MODELY.sk.md)

Each `modely/<ID>.json` file is the manufacturer's **Maximum Heating Capacity (Peak Value)** table for one outdoor unit:
heating capacity (HC, kW) and power input (PI, kW) by outdoor temperature and water flow temperature. The app uses it to calculate
the table-based COP and to calibrate the circuit flow rate.

## Models in the library

| Series | Models | Table range | Source |
|---|---|---|---|
| Split R410A (gen. J) | AE040JXEDEH, AE060JXEDEH, AE090JXEDGH, **AE120JXEDGH** (verified), AE140JXEDGH, AE160JXEDGH | −20…20 °C outdoor, water 25–55 °C | EHS Split Technical Data Book (Europe) |
| Split R32 | AE040RXEDEG, AE060RXEDEG, AE090RXEDEG/GG | −20…20 °C, water 25–55 °C | EHS Split Technical Data Book (Europe) |
| Mono R32 | AE050RXYDEG, AE080RXYDEG/GG, AE120RXYDEG/GG, AE160RXYDEG/GG | −20…20 °C, water 30–65 °C | EHS tank integrated Hydro Unit TDB (Mono, R32) |
| Mono R290 | AE050CXYDEK, AE080CXYDEK/GK, AE120CXYDEK/GK, AE160CXYDEK/GK | −25…35 °C, water 25–75 °C | EHS Mono R290 TDB |
| Mono R290 with integrated pump | AE050CXYBEK, AE080CXYBEK/GK, AE120CXYBEK/GK, AE160CXYBEK/GK | −25…35 °C, water 25–75 °C | EHS Mono R290 Pump TDB |

The exact document title and URL are in the `zdroj` field of each file. The numbers were extracted from the PDFs by a script
(not typed by hand) and spot-checked against the text of the document. **Only AE120JXEDGH is verified on a real unit** – the
others have `"overene": false`. Where the manufacturer gives one table for both the 1-phase and the 3-phase version
(`AE080RXYD*G`), the library has two files with identical values (`…EG` and `…GG`).

Missing: Mono HT Quiet (`AE…BXYD…`) – no publicly available data book with a readable table was found.

## How to add a model

1. In the Samsung **Technical Data Book** for the outdoor unit, find the chapter *Capacity table → Maximum Heating Capacity (Peak Value)*.
2. Create `modely/<DESIGNATION>.json` (file name = ID, only letters, digits and `- _ .`):

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

   - `ta` = outdoor temperatures (rows), `lw` = water flow temperatures (columns), `peak["<ta>"]` = cells `[HC kW, PI kW]`
     in the order of `lw`; a row must have exactly as many cells as `lw`.
   - A cell that the manufacturer does not give is `null` (the first cell of a row must not be `null`).
   - If the table is in watts, convert it to kW. `typ`: `split` / `mono` / `hydrosplit`.
3. Verify: `python3 -c "import profil; print([m['id'] for m in profil.list_models()])"` – the model must be in the list.
4. In the web UI, go to **Settings → Device and home**, select the model, save and restart the services.
5. Set `"overene": true` only after comparing the calculated COP with an actual measurement (electricity meter, heat meter).
