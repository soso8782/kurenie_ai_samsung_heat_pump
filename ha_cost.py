#!/usr/bin/env python3
"""Spotreba domu a náklady na elektrinu do panela Energia v Home Assistant (externé štatistiky `kurenie:spotreba_dom` v kWh
a `kurenie:naklady_elektrina` v EUR).

Zdroj spotreby: (1) jednorazovo stiahnutá história z portálu SSD IMS (ims_hourly.json, 15-min profil zlúčený po hodinách,
od 15. 11. 2023), (2) hodinová štatistika elektromera z integrácie SSD IMS v HA (ha_ws.ENERGY_EXT_ID, s denným oneskorením) –
tá má prednosť tam, kde sú obe. Cena: regulované cenníky SSE pre distribučné územie SSD, sadzba DD5, koncové ceny s DPH.
Elektromer po hodinách nerozlišuje VT/NT, preto sa používa vážená cena podľa podielu VT (ENERGY_VT_SHARE, predvolene 0,09 –
z mesačných odpočtov registrov 1.8.1/1.8.2, sep–nov 2026). Fixná mesačná platba sa rozpočíta na hodiny (12 platieb / 365 dní).
Štatistiky sa vždy prepočítajú celé od prvej hodiny (import je idempotentný). V HA sa zapisujú len tieto dve vlastné štatistiky."""
import calendar, datetime as _dt, json, os, time

STAT_ID = "kurenie:naklady_elektrina"
STAT_NAME = "Náklady na elektrinu (SSE DD5)"
STAT_E_ID = "kurenie:spotreba_dom"
STAT_E_NAME = "Spotreba domu (elektromer SSD)"
IMS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ims_hourly.json")
START = "2023-01-01T00:00:00+00:00"

# (platné od, VT €/kWh, NT €/kWh, mesačná platba €/OM pri HI 3x25 A), koncové ceny s DPH podľa regulovaných cenníkov SSE (SSD):
# 1. 1. 2023 a 1. 1. 2024 (rovnaké), 1. 1. 2025 a 1. 7. 2025 (rovnaké), 1. 1. 2026 a 1. 7. 2026 (rovnaké; od 2026 sa delí
# podľa nároku na adresnú energopomoc, do 2025 platila pre všetky domácnosti jedna regulovaná cena)
TARIFF = {
    "bez": [("2023-01-01", 0.2340631, 0.1245391, 14.1600),
            ("2025-01-01", 0.2333301, 0.1310853, 14.0420),
            ("2026-01-01", 0.2873865, 0.1748244, 12.9234)],
    "s":   [("2023-01-01", 0.2340631, 0.1245391, 14.1600),
            ("2025-01-01", 0.2333301, 0.1310853, 14.0420),
            ("2026-01-01", 0.2502109, 0.1376488, 12.9234)],
}


def _f(cfg, key, default, lo, hi):
    try:
        v = float(str((cfg or {}).get(key) or "").replace(",", "."))
    except ValueError:
        return default
    return v if lo <= v <= hi else default


def tariff_at(cfg, ts):
    """(cena €/kWh vážená podľa podielu VT, fixná platba €/hod) pre hodinu ts. Pred prvým známym cenníkom platí prvý cenník.
    ENERGY_FEE_MONTH v config.env prepíše mesačnú platbu (iný istič) pre všetky obdobia."""
    kind = "s" if str((cfg or {}).get("ENERGY_ENERGOPOMOC", "0")).strip() == "1" else "bez"
    day = time.strftime("%Y-%m-%d", time.localtime(ts))
    rows = TARIFF[kind]
    row = rows[0]
    for r in rows:
        if r[0] <= day:
            row = r
    share = _f(cfg, "ENERGY_VT_SHARE", 0.09, 0.0, 1.0)
    fee = _f(cfg, "ENERGY_FEE_MONTH", row[3], 0.0, 200.0)
    year = time.localtime(ts).tm_year
    days = 366 if calendar.isleap(year) else 365
    return share * row[1] + (1 - share) * row[2], fee * 12.0 / days / 24.0


def load_ims(path=IMS_FILE):
    """{začiatok hodiny: kWh} z jednorazového stiahnutia portálu SSD IMS (alebo prázdny slovník)."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return {int(k): float(v) for k, v in d.items()}
    except (OSError, ValueError, TypeError):
        return {}


def build_rows(cfg, hourly):
    """hourly = {začiatok hodiny: kWh}. Vracia (riadky spotreby, riadky nákladov) {start, state, sum} od prvej po poslednú
    hodinu (hodiny bez údaja = 0 kWh, fixná platba sa účtuje aj tak)."""
    if not hourly:
        return [], []
    a, b = min(hourly), max(hourly)
    er, cr, te, tc = [], [], 0.0, 0.0
    h = a
    while h <= b:
        price, fee = tariff_at(cfg, h)
        kwh = max(0.0, hourly.get(h, 0.0))
        te += kwh
        tc += kwh * price + fee
        iso = _dt.datetime.fromtimestamp(h, _dt.timezone.utc).isoformat()
        er.append({"start": iso, "state": round(te, 4), "sum": round(te, 4)})
        cr.append({"start": iso, "state": round(tc, 4), "sum": round(tc, 4)})
        h += 3600
    return er, cr


def _import(cli, meta, rows):
    for i in range(0, len(rows), 1000):
        cli.call({"type": "recorder/import_statistics", "metadata": meta, "stats": rows[i:i + 1000]}, timeout=120)


def sync(cfg):
    """Prepočíta a naimportuje celé štatistiky spotreby a nákladov. Vracia {"hodin", "kwh", "spolu_eur", "do"} alebo vyhodí výnimku."""
    import ha_ws
    url, token = (cfg.get("HA_URL") or "").strip(), (cfg.get("HA_TOKEN") or "").strip()
    if not url or not token or not ha_ws.ENERGY_EXT_ID:   # bez nastaveného elektromera sa náklady nepočítajú
        return None
    cli = ha_ws.HAClient()
    try:
        cli.connect(url, token, timeout=30)
        res = cli.call({"type": "recorder/statistics_during_period", "start_time": START,
                        "end_time": _dt.datetime.now(_dt.timezone.utc).isoformat(), "statistic_ids": [ha_ws.ENERGY_EXT_ID],
                        "period": "hour", "types": ["change"]}, timeout=120)
        ha = {h: v for h, v in ha_ws._changes((res or {}).get(ha_ws.ENERGY_EXT_ID)).items() if 0 <= v <= ha_ws.MAX_HOUR_KWH}
        hourly = load_ims()
        hourly.update(ha)
        er, cr = build_rows(cfg, hourly)
        if not er:
            return {"hodin": 0, "kwh": 0.0, "spolu_eur": 0.0, "do": None}
        _import(cli, {"statistic_id": STAT_E_ID, "source": "kurenie", "name": STAT_E_NAME, "unit_of_measurement": "kWh",
                      "has_mean": False, "has_sum": True}, er)
        _import(cli, {"statistic_id": STAT_ID, "source": "kurenie", "name": STAT_NAME, "unit_of_measurement": "EUR",
                      "has_mean": False, "has_sum": True}, cr)
        out = {"hodin": len(er), "kwh": er[-1]["sum"], "spolu_eur": cr[-1]["sum"], "do": er[-1]["start"]}
        pr = pump_rows(cfg)
        if pr:
            _import(cli, {"statistic_id": STAT_P_ID, "source": "kurenie", "name": STAT_P_NAME, "unit_of_measurement": "kWh",
                          "has_mean": False, "has_sum": True}, pr)
            out["cerpadlo_kwh"] = pr[-1]["sum"]
        return out
    finally:
        cli.close()


STAT_P_ID = "kurenie:spotreba_cerpadlo"
STAT_P_NAME = "Tepelné čerpadlo (kalibrovaný výpočet)"


def pump_rows(cfg, now=None):
    """Hodinová spotreba čerpadla zo štatistík aplikácie (stats.db: kalibrovaná, inak odhad) ako riadky {start, state, sum};
    len uzavreté hodiny. Pri zmene kalibrácie sa celá história prepočíta (import prepíše staré hodnoty)."""
    import stats
    now = int(now or time.time())
    H = now - now % 3600
    try:
        con = stats._ro(stats._db_path(cfg), 10)
        try:
            rows = con.execute("SELECT ts, COALESCE(kwh_cal, kwh) FROM hourly WHERE ts<? AND kwh IS NOT NULL ORDER BY ts", (H,)).fetchall()
        finally:
            con.close()
    except Exception:
        return []
    out, tot = [], 0.0
    for ts, v in rows:
        tot += max(0.0, v or 0.0)
        iso = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).isoformat()
        out.append({"start": iso, "state": round(tot, 4), "sum": round(tot, 4)})
    return out
