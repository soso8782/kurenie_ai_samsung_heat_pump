#!/usr/bin/env python3
"""Spätné doplnenie hodinových štatistík z Home Assistanta do stats.db (SPEC_STATS.md §5). Jednorazový skript.

Použitie (v priečinku kurenia):
    python3 stats_backfill.py [--from YYYY-MM-DD] [--dry-run] [--chunk-days 60]

Číta iba z Home Assistanta (recorder/statistics_during_period). Zapisuje len do tabuliek `hourly` a `meta`:
  - hodina bez riadku -> vloží sa riadok src='ha', n NULL;
  - hodina s riadkom z tc/mix -> doplnia sa LEN stĺpce, ktoré sú NULL, src='mix';
  - nenulové hodnoty sa nikdy neprepisujú (ani pri tc hodinách s n>=20);
  - opakované spustenie nič nezdvojí. --dry-run nezapíše nič (pracuje nad kópiou v pamäti).
"""
import argparse
import datetime
import os
import sqlite3
import sys
import time
import urllib.parse

if "TZ" not in os.environ and hasattr(time, "tzset"):
    os.environ["TZ"] = "Europe/Bratislava"      # pásmo databázy hodín; pri explicitnom TZ sa nemení
    time.tzset()

import ha_ws
from common import ROOT, load_cfg

TC_PREFIX = "sensor.technicka_miestnost_m5stack_tc_"
# stĺpec -> štatistické id v HA v poradí priority (prvé id s hodnotou v danej hodine vyhráva)
COLUMN_IDS = [
    ("vonku", ["sensor.irimav20_temperature", "sensor.m5stack_tc_vonkajsia_teplota_2"]),
    # riadiaca detská: ESPresense (nové čidlo), pre staršie obdobie náhradné Zigbee čidlo (o ~0,3 °C iné)
    ("detska", ["sensor.detska_izba_espresense_detska_izba_detska_izba_teplota", "sensor.detska_t_h_temperature"]),
    ("jedalen", ["sensor.0x00124b0029114db8_temperature"]),
    ("kuchyna", ["sensor.t_h_kuchyna_temperature"]),
    ("obyvacka", ["sensor.obyvacka_t_h_cidlo_temperature"]),
    ("spalna", ["sensor.spalna_t_h_cidlo_2_kruhovy_temperature"]),
    ("pracovna", ["sensor.pracovna_t_h_cidlo_temperature"]),
    ("kupelna", ["sensor.kupelna_t_h_temperature"]),
    ("chodba", ["sensor.t_h_sensor_chodba_teplota"]),
    ("kotolna", ["sensor.t_h_kotolna_teplota"]),
    ("tw2", [TC_PREFIX + "vystupna_teplota_vody_tw2"]),
    ("tw1", [TC_PREFIX + "spiatocka_tw1"]),
    ("wl", [TC_PREFIX + "cielova_ekvitermicka_voda_wl"]),
    ("tuv", [TC_PREFIX + "teplota_tuv_bojler"]),
]
VALUE_COLS = [c for c, _ in COLUMN_IDS] + ["vonku_min", "vonku_max", "kwh_dom"]
ALL_IDS = list(dict.fromkeys(i for _, ids in COLUMN_IDS for i in ids))
NEW_COLS = (("kwh_dom", "REAL"), ("kwh_cal", "REAL"), ("q_kwh", "REAL"), ("cop", "REAL"))   # pridané neskôr (SPEC_ENERGIA)

HOURLY_DDL = """CREATE TABLE IF NOT EXISTS hourly(
  ts INTEGER PRIMARY KEY, n INTEGER, src TEXT,
  vonku REAL, vonku_min REAL, vonku_max REAL,
  ciel REAL, detska REAL,
  jedalen REAL, kuchyna REAL, obyvacka REAL, spalna REAL, pracovna REAL, kupelna REAL, chodba REAL, kotolna REAL,
  tw2 REAL, tw1 REAL, wl REAL, dt_beh REAL,
  tuv REAL, tuv_ciel REAL, tuv_min REAL,
  beh_min REAL, kurenie_min REAL, odmraz_min REAL, starty INTEGER, hz_avg REAL, hz_max REAL,
  kwh REAL, prikon REAL,
  updated INTEGER, kwh_dom REAL, kwh_cal REAL, q_kwh REAL, cop REAL)"""
META_DDL = "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)"


def migrate(conn):
    """Idempotentne doplní stĺpce pridané neskôr (staršia stats.db ich nemá)."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(hourly)")}
    for col, typ in NEW_COLS:
        if col not in have:
            try:
                conn.execute("ALTER TABLE hourly ADD COLUMN %s %s" % (col, typ))
            except sqlite3.OperationalError:
                pass


def fail(msg, code=2):
    print(msg, file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------- časy

def ha_time_ts(v):
    """Čas z HA: epoch milisekundy, epoch sekundy alebo ISO reťazec -> unix sekundy (float). Iné = ValueError."""
    if isinstance(v, bool):
        raise ValueError("neplatný čas")
    if isinstance(v, (int, float)):
        x = float(v)
    elif isinstance(v, str):
        s = v.strip()
        try:
            x = float(s)
        except ValueError:
            d = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=datetime.timezone.utc)
            return d.timestamp()
    else:
        raise ValueError("neplatný čas")
    return x / 1000.0 if abs(x) >= 1e11 else x


def local_hour_start(ts):
    """Začiatok lokálnej hodiny, v ktorej leží okamih ts (unix s). Posun pásma sa berie pre ten konkrétny okamih,
    takže hodina 02:00 v noci zmeny času (2 výskyty) ostane dve rôzne hodiny. Vždy celé sekundy."""
    t = int(ts // 1)
    off = time.localtime(t).tm_gmtoff
    if off is None:                          # bez údaja o posune: predpokladáme celohodinový posun
        return t - t % 3600
    wall = t + off
    return wall - wall % 3600 - off


def local_midnight(d):
    return int(time.mktime((d.year, d.month, d.day, 0, 0, 0, 0, 0, -1)))


def iso_utc(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()


def fmt_local(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def make_chunks(d_from, end_ts, days):
    """Lokálne polnoci ohraničené bloky [a, b) od d_from do end_ts (end_ts je hranica hodiny)."""
    out, d = [], d_from
    while True:
        a = local_midnight(d)
        if a >= end_ts:
            break
        d2 = d + datetime.timedelta(days=days)
        out.append((a, min(local_midnight(d2), end_ts)))
        d = d2
    return out


# ---------------------------------------------------------------- hodnoty z HA

def _num(x):
    if x is None or isinstance(x, bool):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class Stats:
    def __init__(self):
        self.with_data = set()     # id, ktoré vrátili aspoň jednu hodnotu mean
        self.span = {}             # stĺpec -> [prvá hodina, posledná hodina, počet hodnôt z HA]
        self.bad_rows = 0          # riadky s nečitateľným časom
        self.dom_src = None        # zdroj kwh_dom (ssd_ims | tarify)


def resolve(res, stats):
    """Odpoveď HA pre jeden blok -> {hodina_ts: {stĺpec: hodnota}}. Prvé id v poradí COLUMN_IDS s hodnotou vyhráva."""
    vals, won = {}, set()
    for col, ids in COLUMN_IDS:                 # poradie stĺpcov a id je priorita
        for sid in ids:
            for row in res.get(sid) or []:
                if not isinstance(row, dict):
                    stats.bad_rows += 1
                    continue
                mean = _num(row.get("mean"))
                if mean is None:
                    continue
                stats.with_data.add(sid)
                try:
                    ts = local_hour_start(ha_time_ts(row.get("start")))
                except (ValueError, TypeError, OverflowError, OSError):
                    stats.bad_rows += 1
                    continue
                if (col, ts) in won:             # už vyhralo skoršie id v poradí
                    continue
                won.add((col, ts))
                h = vals.setdefault(ts, {})
                h[col] = mean
                if col == "vonku":                # min/max z rovnakého zdroja ako priemer
                    h["vonku_min"] = _num(row.get("min"))
                    h["vonku_max"] = _num(row.get("max"))
    for ts, h in vals.items():
        for c, v in h.items():
            if v is not None:
                sp = stats.span.setdefault(c, [ts, ts, 0])
                sp[0], sp[1] = min(sp[0], ts), max(sp[1], ts)
                sp[2] += 1
    return vals


def merge_dom(vals, dom, stats):
    """Pridá hodinovú spotrebu celého domu (kWh, z ha_ws.energy_hourly) do vals ako stĺpec kwh_dom."""
    d, info = dom if isinstance(dom, tuple) else (dom, {})
    for ts, v in (d or {}).items():
        if v is None or not v > 0:
            continue
        vals.setdefault(ts, {})["kwh_dom"] = float(v)
        sp = stats.span.setdefault("kwh_dom", [ts, ts, 0])
        sp[0], sp[1], sp[2] = min(sp[0], ts), max(sp[1], ts), sp[2] + 1
    if isinstance(info, dict) and info.get("zdroj"):
        stats.dom_src = info["zdroj"]


# ---------------------------------------------------------------- zápis

def write_hours(conn, vals, now):
    """Zapíše hodiny podľa pravidiel. Vracia (vložené riadky, doplnené riadky, {stĺpec: počet zapísaných hodnôt})."""
    ins = upd = 0
    filled = {c: 0 for c in VALUE_COLS}
    cols_sql = ", ".join(VALUE_COLS)
    for ts in sorted(vals):
        new = {k: v for k, v in vals[ts].items() if v is not None}
        if not new:
            continue
        row = conn.execute("SELECT src, %s FROM hourly WHERE ts=?" % cols_sql, (ts,)).fetchone()
        if row is None:
            cols = list(new)
            conn.execute("INSERT INTO hourly(ts, n, src, updated, %s) VALUES(?, NULL, 'ha', ?, %s)"
                         % (", ".join(cols), ", ".join("?" * len(cols))), (ts, now, *[new[c] for c in cols]))
            ins += 1
            for c in cols:
                filled[c] += 1
            continue
        src = row[0]
        cur = dict(zip(VALUE_COLS, row[1:]))
        fill = {c: v for c, v in new.items() if cur.get(c) is None}   # nenulové hodnoty sa neprepisujú
        if not fill:
            continue
        new_src = "ha" if src == "ha" else "mix"
        sets = ", ".join("%s=?" % c for c in fill)
        conn.execute("UPDATE hourly SET %s, src=?, updated=? WHERE ts=?" % sets,
                     (*fill.values(), new_src, now, ts))
        upd += 1
        for c in fill:
            filled[c] += 1
    return ins, upd, filled


def open_db(path, dry):
    """Dry-run: kópia databázy v pamäti (súbor sa neotvára na zápis). Inak skutočná stats.db."""
    if dry:
        mem = sqlite3.connect(":memory:")
        if os.path.exists(path):
            src = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(path), uri=True, timeout=30)
            try:
                src.backup(mem)
            finally:
                src.close()
        mem.executescript(HOURLY_DDL + ";" + META_DDL + ";")
        migrate(mem)
        return mem
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(HOURLY_DDL + ";" + META_DDL + ";")
    migrate(conn)
    conn.commit()
    return conn


# ---------------------------------------------------------------- hlavný tok

def main(argv=None):
    ap = argparse.ArgumentParser(description="Spätné doplnenie hodín z Home Assistanta do stats.db.")
    ap.add_argument("--from", dest="date_from", default="2025-09-01", metavar="YYYY-MM-DD",
                    help="prvý deň (lokálny), predvolene 2025-09-01")
    ap.add_argument("--dry-run", action="store_true", help="nič nezapíše, len vypíše, čo by sa zapísalo")
    ap.add_argument("--chunk-days", type=int, default=60, help="dní v jednej požiadavke (predvolene 60)")
    args = ap.parse_args(argv)

    try:
        d_from = datetime.datetime.strptime(args.date_from, "%Y-%m-%d").date()
    except ValueError:
        fail("Neplatný dátum v --from (očakávam YYYY-MM-DD).")
    if not 1 <= args.chunk_days <= 366:
        fail("--chunk-days musí byť od 1 do 366.")

    cfg = load_cfg()
    url = (cfg.get("HA_URL") or "").strip()
    token = (cfg.get("HA_TOKEN") or "").strip()
    if not token:
        fail("V config.env chýba HA_TOKEN, nie je čím sa prihlásiť do Home Assistanta.")
    db_path = cfg.get("STATS_DB") or os.path.join(ROOT, "stats.db")

    now = int(time.time())
    end_ts = local_hour_start(now)           # rozpracovanú aktuálnu hodinu nečítame
    chunks = make_chunks(d_from, end_ts, args.chunk_days)
    if not chunks:
        print("Nič na doplnenie: začiatok %s je nie pred poslednou ukončenou hodinou." % args.date_from)

    print("Databáza: %s%s" % (db_path, " (skúška: nič sa nezapíše)" if args.dry_run else ""))
    conn = open_db(db_path, args.dry_run)
    stats = Stats()
    ins_total = upd_total = 0
    filled_total = {c: 0 for c in VALUE_COLS}
    cli = ha_ws.HAClient()
    try:
        cli.connect(url, token, timeout=30)
        print("Pripojené k Home Assistantu (%s). Blokov: %d." % (url, len(chunks)))
        for i, (a, b) in enumerate(chunks, 1):
            res = cli.call({
                "type": "recorder/statistics_during_period",
                "start_time": iso_utc(a),
                "end_time": iso_utc(b),
                "statistic_ids": ALL_IDS,
                "period": "hour",
                "types": ["mean", "min", "max"],
            })
            if not isinstance(res, dict):
                raise ha_ws.HAError("Neočakávaný tvar odpovede na štatistiky.")
            vals = resolve(res, stats)
            merge_dom(vals, ha_ws.energy_hourly(cli, a, b), stats)
            ins, upd, filled = write_hours(conn, vals, now)
            conn.commit()
            ins_total += ins
            upd_total += upd
            for c in VALUE_COLS:
                filled_total[c] += filled[c]
            print("Blok %d/%d %s – %s: hodín s dátami %d, vložených %d, doplnených %d"
                  % (i, len(chunks), fmt_local(a)[:10], fmt_local(b)[:10], len(vals), ins, upd))
    except ha_ws.HAError as e:
        conn.rollback()
        print("CHYBA: %s" % e, file=sys.stderr)
        print("Doplnenie je prerušené; opakované spustenie pokračuje bez duplicít.", file=sys.stderr)
        return 1
    finally:
        cli.close()

    if not args.dry_run:
        conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('backfill_ha_done', ?)", (str(int(time.time())),))
        conn.commit()
    conn.close()

    print("")
    print("SÚHRN")
    print("Hodín vložených (nový riadok, src='ha'): %d" % ins_total)
    print("Existujúcich hodín doplnených (NULL stĺpce): %d" % upd_total)
    print("Štatistické ID bez dát: %s" % (", ".join(i for i in ALL_IDS if i not in stats.with_data) or "žiadne"))
    print("Stĺpce (počet doplnených hodnôt, prvá a posledná hodina s dátami z HA):")
    for c in VALUE_COLS:
        sp = stats.span.get(c)
        rng = ("%s – %s" % (fmt_local(sp[0]), fmt_local(sp[1]))) if sp else "bez dát"
        print("  %-11s %7d  %s" % (c, filled_total[c], rng))
    print("Spotreba celého domu (kwh_dom) z: %s" % (stats.dom_src or "žiadny zdroj nemal údaje"))
    if stats.bad_rows:
        print("Riadky s nečitateľným časom alebo tvarom (preskočené): %d" % stats.bad_rows)
    if args.dry_run:
        print("Skúška: nič nebolo zapísané.")
    else:
        print("Zapísané. meta.backfill_ha_done nastavená.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
