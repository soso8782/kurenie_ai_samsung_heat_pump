# Model library (`modely/`)

[![English](https://img.shields.io/badge/lang-English-blue.svg)](MODELY.md) [![Slovenčina](https://img.shields.io/badge/jazyk-Sloven%C4%8Dina-green.svg)](MODELY.sk.md)

Each `modely/<ID>.json` file is the manufacturer's **Maximum Heating Capacity (Peak Value)** table for one outdoor unit:
heating capacity (HC, kW) and power input (PI, kW) by outdoor temperature and water flow temperature. The app uses it to calculate
the table-based COP and to calibrate the circuit flow rate.

## Models in the library

| Series | Models | Table range | Source |
|---|---|---|---|
| Split R410A (gen. J) | AE040JXEDEH, AE060JXEDEH, AE090JXEDGH, **AE120JXEDGH** (verified), AE140JXEDGH, AE160JXEDGH | −20…20 °C outdoor, water 25–55 °C | EHS Split Technical Data Book (Europe) |
| Split R410A (series A) | AE120AXEDEH/GH, AE160AXEDEH/GH | −25…35 °C, water 25–55 °C | EHS Split for Europe (R410A) TDB Ver. 1.1 |
| Split R32 | AE040RXEDEG, AE060RXEDEG, AE090RXEDEG/GG | −20…20 °C, water 25–55 °C | EHS Split Technical Data Book (Europe) |
| Split R32 (DXED, 12.5–16 kW) | AE125DXEDEG/GG, AE160DXEDEG/GG | −25…35 °C, water 25–65 °C | TDB 2024 EHS Split for Europe Ver. 1.1 |
| Mono R410A (gen. J) | AE050JXYDEH, AE090JXYDEH/GH, AE120JXYDEH/GH, AE140JXYDEH/GH, AE160JXYDEH/GH | −20…20 °C, water 30–55 °C | EHS Mono 50Hz TDB Ver. 1.5 |
| Mono R32 | AE050RXYDEG, AE080RXYDEG/GG, AE120RXYDEG/GG, AE160RXYDEG/GG | −20…20 °C, water 30–65 °C | EHS tank integrated Hydro Unit TDB (Mono, R32) |
| Mono HT Quiet R32 | AE080BXYDEG/GG, AE120BXYDEG/GG, AE140BXYDEG/GG | −30…43 °C, water 25–70 °C | EHS Mono HT Quiet TDB Ver. 2.1 |
| Mono R290 | AE050CXYDEK, AE080CXYDEK/GK, AE120CXYDEK/GK, AE160CXYDEK/GK | −25…35 °C, water 25–75 °C | EHS Mono R290 TDB |
| Mono R290 with integrated pump | AE050CXYBEK, AE080CXYBEK/GK, AE120CXYBEK/GK, AE160CXYBEK/GK | −25…35 °C, water 25–75 °C | EHS Mono R290 Pump TDB |

The exact document title and URL are in the `zdroj` field of each file. The numbers were extracted from the PDFs by a script
(not typed by hand) and spot-checked against the text of the document. **Only AE120JXEDGH is verified on a real unit** – the
others have `"overene": false`. Where the manufacturer gives one table for both the 1-phase and the 3-phase version
(`AE080RXYD*G`), the library has two files with identical values (`…EG` and `…GG`).

Series A (`AE…AXED…H`): in the PDF the cell 23.09/5.07 is copied into the LWT 40 °C column at 30 and 35 °C outdoor; those
cells are `null`. Tables with a water-flow column (HC, PI, WF) were read in triples.

Mono HT Quiet R32 exists only as 8, 12 and 14 kW (both data books list no 16 kW unit); for 16 kW use the Mono R290
(`AE160CXY*K`) or the standard Mono R32 (`AE160RXYD*G`).

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
