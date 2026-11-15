#!/usr/bin/env python3
"""Testy SPEC_ENERGIA: kalibrácia spotreby čerpadla, COP, kwh_dom, migrácia schémy, live. Syntetické dáta, dočasné DB.
Spustenie: python3 t_energia.py   (v priečinku kurenia; nič nezapisuje do ostrých databáz)"""
import os, shutil, sqlite3, sys, tempfile, time, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stats
import ha_ws

HOUR = 3600


def mid(y, m, d):
    return int(time.mktime((y, m, d, 0, 0, 0, 0, 0, -1)))


class Env:
    def setUpEnv(self):
        self.dir = tempfile.mkdtemp(prefix="t_energia_")
        self.cfg = {"TC_DB": os.path.join(self.dir, "tc.db"), "STATS_DB": os.path.join(self.dir, "stats.db"),
                    "AGENT_DB": os.path.join(self.dir, "none.db")}
        tc = sqlite3.connect(self.cfg["TC_DB"])
        tc.executescript("CREATE TABLE samples(ts INTEGER, entity TEXT, value REAL); CREATE INDEX samples_ts ON samples(ts);"
                         "CREATE TABLE events(ts INTEGER, kind TEXT, entity TEXT, old TEXT, new TEXT, text TEXT);")
        tc.commit()
        self.tc = tc

    def tearDownEnv(self):
        self.tc.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def minutes(self, start, n, freq=40.0, tw2=40.0, tw1=35.0, flow=15.0, kwh_min=0.04, prikon=2400.0, kwh0=0.0, vonku=0.0):
        """n minút vzoriek od start: kompresor beží (freq), prietok flow, počítadlo kWh rastie o kwh_min za minútu."""
        rows = []
        kw = kwh0
        for i in range(n):
            ts = start + 60 * i
            kw += kwh_min
            rows += [(ts, stats.TC["vonku"], vonku), (ts, stats.TC["freq"], freq), (ts, stats.TC["tw2"], tw2),
                     (ts, stats.TC["tw1"], tw1), (ts, stats.TC["kwh"], kw), (ts, stats.TC["prikon"], prikon)]
            if flow is not None:
                rows.append((ts, stats.TC["flow"], flow))
        self.tc.executemany("INSERT INTO samples VALUES(?,?,?)", rows)
        self.tc.commit()
        return kw

    def hourly_row(self, ts):
        con = stats.connect(self.cfg)
        try:
            con.row_factory = sqlite3.Row
            return con.execute("SELECT * FROM hourly WHERE ts=?", (ts,)).fetchone()
        finally:
            con.close()


def synth_rows(days, k_true, base_fn, idle_hours=(), start=None, dom_none=(), idle_fn=None):
    """Hodinové riadky (ts, beh_min, kwh, kwh_dom): hodiny v idle_hours bez chodu, inak beh 60 min a kwh podľa hodiny."""
    start = start or mid(2026, 1, 5)
    rows = []
    for d in range(days):
        day0 = mid(2026, 1, 5 + d)
        for h in range(24):
            ts = day0 + h * HOUR
            if h in idle_hours or (idle_fn and idle_fn(d, h)):
                kwh, beh = 0.0, 0.0
            else:
                kwh, beh = 1.0 + 0.1 * (h % 7) + 0.05 * d, 60.0
            dom = base_fn(h) + k_true * kwh
            rows.append((ts, beh, kwh, None if ts in dom_none else dom))
    return rows


class Calibration(unittest.TestCase):
    NOW = mid(2026, 1, 20)

    def test_known_k(self):
        # základ domu v noci (0–4 h) stály: nočná kalibrácia (SPEC_cop) inak zamieňa hodinový základ so spotrebou čerpadla
        base = lambda h: 0.2 + 0.02 * max(h, 5)
        rows = synth_rows(20, 0.9, base, idle_fn=lambda d, h: (d + h) % 3 == 0)      # pre každú hodinu dňa >= 5 hodín bez chodu
        res = stats.calibration_calc(rows, mid(2026, 1, 30))
        self.assertAlmostEqual(res["k"], 0.9, delta=0.02)
        self.assertEqual(res["n_dni"], 20)
        self.assertLess(res["mape"], 3.0)
        self.assertIn("median", res["base_zdroj"])

    def test_too_few_days(self):
        rows = synth_rows(2, 1.0, lambda h: 0.3, idle_hours=set(range(0, 8)))
        res = stats.calibration_calc(rows, self.NOW)
        self.assertIsNone(res["k"])
        self.assertEqual(res["n_dni"], 2)
        self.assertTrue(res["dovod"])

    def test_clamp(self):
        rows = synth_rows(6, 2.5, lambda h: 0.2, idle_hours=set(range(0, 8)))
        res = stats.calibration_calc(rows, self.NOW)
        self.assertEqual(res["k"], stats.K_HI)
        self.assertGreater(res["k_raw"], 2.0)
        rows = synth_rows(6, 0.2, lambda h: 0.2, idle_hours=set(range(0, 8)))
        self.assertEqual(stats.calibration_calc(rows, self.NOW)["k"], stats.K_LO)

    def test_null_beh_not_used_for_base(self):
        # hodiny s beh_min NULL (iba HA) nesmú ísť do základu; bez hodín bez chodu a s málo údajmi k = None
        rows = [(ts, None, None, 0.3) for ts in range(self.NOW - 40 * 3600, self.NOW, 3600)]
        res = stats.calibration_calc(rows, self.NOW)
        self.assertIsNone(res["k"])
        self.assertEqual(res["base_hodin"], 0)

    def test_fallback_night_median_for_sparse_hours(self):
        # pre hodiny dňa s < 5 hodinami bez chodu sa použije celkový medián nočných hodín bez chodu
        rows = synth_rows(8, 1.0, lambda h: 0.25 if h < 6 else 0.6, idle_hours={0, 1, 2, 3, 4, 5})
        res = stats.calibration_calc(rows, self.NOW)
        self.assertIsNotNone(res["k"])
        self.assertIn("náhradný nočný medián", res["base_zdroj"])
        # základ pre dopoludnie bol nahradený nočným (0,25), preto je k nad skutočným 1,0 (0,6 sa nepoužilo)
        self.assertGreater(res["k"], 1.0)

    def test_regression_when_always_running(self):
        base = lambda h: 0.3
        rows = synth_rows(8, 1.1, base, idle_hours=())
        res = stats.calibration_calc(rows, self.NOW)
        self.assertIn("regresia", res["base_zdroj"])
        self.assertAlmostEqual(res["k"], 1.1, delta=0.05)

    def test_recalibrate_writes_meta_and_kwh_cal(self):
        e = Env()
        e.setUpEnv()
        try:
            now = mid(2026, 1, 20)
            rows = synth_rows(8, 0.9, lambda h: 0.3, idle_hours=set(range(0, 6)))
            con = stats.connect(e.cfg)
            for ts, beh, kwh, dom in rows:
                con.execute("INSERT INTO hourly(ts, n, src, beh_min, kwh, kwh_dom, q_kwh, updated) VALUES(?,60,'tc',?,?,?,?,?)",
                            (ts, beh, kwh, dom, 2.0 * kwh if kwh else None, ts))
            con.commit()
            con.close()
            res = stats.recalibrate(e.cfg, now=now)
            self.assertAlmostEqual(res["k"], 0.9, delta=0.02)
            con = stats.connect(e.cfg)
            self.assertAlmostEqual(stats.get_k(con), res["k"], places=4)
            r = con.execute("SELECT kwh, kwh_cal, q_kwh, cop FROM hourly WHERE kwh > 1 LIMIT 1").fetchone()
            self.assertAlmostEqual(r[1], r[0] * res["k"], places=2)
            self.assertAlmostEqual(r[3], r[2] / r[1], places=2)           # COP po kalibrácii = q / kwh_cal
            con.close()
            cov = stats.coverage(e.cfg)
            self.assertEqual(cov["kalibracia"]["n_dni"], 8)
            self.assertTrue(cov["kalibracia"]["vypocitane"])
            self.assertIsNotNone(cov["first_day"]["kwh_dom"])
            self.assertIsNotNone(cov["first_day"]["cop"])
        finally:
            e.tearDownEnv()


class CopAgg(unittest.TestCase, Env):
    def setUp(self):
        self.setUpEnv()
        self.t0 = mid(2026, 1, 10)

    def tearDown(self):
        self.tearDownEnv()

    def agg(self, a, b):
        stats.aggregate_range(self.cfg, a, b)

    def test_cop_basic(self):
        # P_th = 15/60*4,186*5 = 5,2325 kW, el. 0,04 kWh/min = 2,4 kW  ->  COP = 2,18
        self.minutes(self.t0, 120)
        self.agg(self.t0, self.t0 + 2 * HOUR)
        r = self.hourly_row(self.t0 + HOUR)
        self.assertAlmostEqual(r["q_kwh"], 5.2325, delta=0.02)
        self.assertAlmostEqual(r["kwh"], 2.4, delta=0.05)
        self.assertAlmostEqual(r["cop"], 5.2325 / 2.4, delta=0.05)
        self.assertIsNone(r["kwh_cal"])                      # bez kalibrácie

    def test_cop_uses_kwh_cal_when_calibrated(self):
        con = stats.connect(self.cfg)
        stats._meta_set(con, "kalibracia", '{"k": 0.8, "ts": 1}')
        con.commit()
        con.close()
        self.minutes(self.t0, 120)
        self.agg(self.t0, self.t0 + 2 * HOUR)
        r = self.hourly_row(self.t0 + HOUR)
        self.assertAlmostEqual(r["kwh_cal"], r["kwh"] * 0.8, places=3)
        self.assertAlmostEqual(r["cop"], r["q_kwh"] / r["kwh_cal"], places=2)

    def test_invalid_flow_sentinel_gives_no_cop(self):
        self.minutes(self.t0, 120, flow=6553.5)
        self.agg(self.t0, self.t0 + 2 * HOUR)
        r = self.hourly_row(self.t0 + HOUR)
        self.assertIsNone(r["q_kwh"])
        self.assertIsNone(r["cop"])
        self.assertIsNotNone(r["kwh"])

    def test_fixed_flow_fallback_from_config(self):
        self.minutes(self.t0, 120, flow=None)
        self.agg(self.t0, self.t0 + 2 * HOUR)
        self.assertIsNone(self.hourly_row(self.t0 + HOUR)["q_kwh"])
        self.cfg["STATS_FLOW_LPM"] = "15"
        self.agg(self.t0, self.t0 + 2 * HOUR)
        self.assertAlmostEqual(self.hourly_row(self.t0 + HOUR)["q_kwh"], 5.2325, delta=0.02)

    def test_negative_delta_t_counts_zero(self):
        self.minutes(self.t0, 60, tw2=30.0, tw1=35.0)        # odmrazovanie: spiatočka teplejšia
        self.agg(self.t0, self.t0 + HOUR)
        self.assertEqual(self.hourly_row(self.t0)["q_kwh"], 0.0)

    def test_low_coverage_gives_null(self):
        # prietok len pre 30 % minút bežiaceho kompresora -> pokrytie pod 70 %
        self.minutes(self.t0, 60, flow=None)
        self.tc.executemany("INSERT INTO samples VALUES(?,?,?)", [(self.t0 + 60 * i, stats.TC["flow"], 15.0) for i in range(18)])
        self.tc.commit()
        self.agg(self.t0, self.t0 + HOUR)
        self.assertIsNone(self.hourly_row(self.t0)["q_kwh"])

    def test_partial_coverage_scaled_to_run_minutes(self):
        self.minutes(self.t0, 60, flow=None)
        self.tc.executemany("INSERT INTO samples VALUES(?,?,?)", [(self.t0 + 60 * i, stats.TC["flow"], 15.0) for i in range(48)])
        self.tc.commit()
        self.agg(self.t0, self.t0 + HOUR)
        self.assertAlmostEqual(self.hourly_row(self.t0)["q_kwh"], 5.2325, delta=0.05)

    def test_low_kwh_no_cop(self):
        self.minutes(self.t0, 60, kwh_min=0.0005)            # < 0,05 kWh za hodinu
        self.agg(self.t0, self.t0 + HOUR)
        r = self.hourly_row(self.t0)
        self.assertIsNotNone(r["q_kwh"])
        self.assertIsNone(r["cop"])

    def test_compressor_off_minutes_ignored(self):
        self.minutes(self.t0, 30)
        self.minutes(self.t0 + 1800, 30, freq=0.0, tw2=40.0, tw1=35.0, kwh_min=0.0, kwh0=1.2)
        self.agg(self.t0, self.t0 + HOUR)
        self.assertAlmostEqual(self.hourly_row(self.t0)["q_kwh"], 5.2325 / 2, delta=0.05)

    def test_day_cop_is_ratio_of_sums_not_mean_of_hourly(self):
        # hodina 1: q = 5,23, kwh = 2,4 (COP 2,18); hodina 2: dvojnásobný prietok, rovnaká spotreba (q = 10,47, COP 4,36)
        self.minutes(self.t0, 60)
        self.minutes(self.t0 + HOUR, 60, flow=30.0, kwh0=2.4)
        self.agg(self.t0, self.t0 + 2 * HOUR)
        s = stats.get_range(self.cfg, "2026-01-10", "2026-01-10", "day")["rows"][0]
        qsum = 5.2325 * 3
        self.assertAlmostEqual(s["cop"], qsum / 4.8, delta=0.05)
        mean_h = (5.2325 / 2.4 + 10.465 / 2.4) / 2
        self.assertAlmostEqual(s["cop"], mean_h, delta=0.05)  # rovnaká spotreba -> zhodné; nižšie rozdielna spotreba
        self.assertEqual(s["kwh_zdroj"], "odhad")

    def test_ratio_vs_mean_differ_when_kwh_differs(self):
        self.minutes(self.t0, 60, kwh_min=0.01)              # kwh 0,6, q 5,23 -> COP 8,7
        self.minutes(self.t0 + HOUR, 60, kwh_min=0.05, kwh0=0.6)   # kwh 3,0, q 5,23 -> COP 1,74
        self.agg(self.t0, self.t0 + 2 * HOUR)
        s = stats.get_day(self.cfg, "2026-01-10")["summary"]
        self.assertAlmostEqual(s["cop"], 2 * 5.2325 / 3.6, delta=0.05)           # 2,91, nie 5,2
        self.assertGreater((5.2325 / 0.6 + 5.2325 / 3.0) / 2, 4.5)

    def test_pure_mode_cop(self):
        # hodina kúrenia (ventil Room) a hodina TÚV (ventil Tank) s rôznym prietokom
        self.tc.executemany("INSERT INTO events VALUES(?,?,?,?,?,?)", [
            (self.t0 - 60, "ovladanie", stats.VALVE, "", "Room (Heating)", ""),
            (self.t0 + HOUR, "ovladanie", stats.VALVE, "", "Tank (DHW)", "")])
        self.minutes(self.t0, 60)
        self.minutes(self.t0 + HOUR, 60, flow=30.0, kwh0=2.4)
        self.agg(self.t0, self.t0 + 2 * HOUR)
        s = stats.get_day(self.cfg, "2026-01-10")["summary"]
        self.assertAlmostEqual(s["cop_kurenie"], 5.2325 / 2.4, delta=0.05)
        self.assertAlmostEqual(s["cop_tuv"], 10.465 / 2.4, delta=0.05)
        self.assertEqual(s["cop_kurenie_hodin"], 1)

    def test_metrics_and_csv_include_new_keys(self):
        keys = [m["key"] for m in stats.METRICS]
        for k in ("q_kwh", "cop", "kwh_dom", "kwh_cal"):
            self.assertIn(k, keys)
        self.minutes(self.t0, 60)
        self.agg(self.t0, self.t0 + HOUR)
        for kind, kw in (("day", {"date": "2026-01-10"}), ("range", {"date_from": "2026-01-10", "date_to": "2026-01-10"}),
                         ("compare", {"dates": "2026-01-10"})):
            txt = stats.export_csv(self.cfg, kind, **kw)
            self.assertIn("COP", txt.split("\r\n")[0])
            self.assertIn("Dom spolu", txt.split("\r\n")[0])
            self.assertIn("Vyrobené teplo", txt.split("\r\n")[0])
        c = stats.compare(self.cfg, ["2026-01-10"])
        self.assertIn("cop", c["dates"][0]["summary"])
        self.assertIn("q_kwh", c["dates"][0]["hours"][0])


class DomAndMigration(unittest.TestCase, Env):
    def setUp(self):
        self.setUpEnv()

    def tearDown(self):
        self.tearDownEnv()

    def test_migration_adds_columns_and_keeps_data(self):
        old = sqlite3.connect(self.cfg["STATS_DB"])
        old.executescript("""CREATE TABLE hourly(ts INTEGER PRIMARY KEY, n INTEGER, src TEXT, vonku REAL, vonku_min REAL, vonku_max REAL,
          ciel REAL, detska REAL, jedalen REAL, kuchyna REAL, obyvacka REAL, spalna REAL, pracovna REAL, kupelna REAL, chodba REAL,
          kotolna REAL, tw2 REAL, tw1 REAL, wl REAL, dt_beh REAL, tuv REAL, tuv_ciel REAL, tuv_min REAL, beh_min REAL, kurenie_min REAL,
          odmraz_min REAL, starty INTEGER, hz_avg REAL, hz_max REAL, kwh REAL, prikon REAL, updated INTEGER);
          CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT); INSERT INTO meta VALUES('schema','1');
          INSERT INTO hourly(ts, n, src, vonku, kwh) VALUES(1000, 60, 'tc', 3.5, 1.25);""")
        old.commit()
        old.close()
        con = stats.connect(self.cfg)                          # migrácia pri otvorení
        cols = {r[1] for r in con.execute("PRAGMA table_info(hourly)")}
        self.assertTrue({"kwh_dom", "kwh_cal", "q_kwh", "cop"} <= cols)
        self.assertEqual(con.execute("SELECT vonku, kwh FROM hourly WHERE ts=1000").fetchone(), (3.5, 1.25))
        con.close()
        stats.connect(self.cfg).close()                        # opakované otvorenie je v poriadku
        self.assertTrue(stats.coverage(self.cfg)["hours"] >= 1)

    def test_apply_kwh_dom_fills_null_only(self):
        con = stats.connect(self.cfg)
        con.execute("INSERT INTO hourly(ts, n, src, vonku, kwh_dom, updated) VALUES(7200, 60, 'tc', 1.0, 3.0, 1)")
        con.execute("INSERT INTO hourly(ts, n, src, vonku, updated) VALUES(10800, 60, 'tc', 1.0, 1)")
        con.commit()
        ins, upd = stats.apply_kwh_dom(con, {7200: 9.9, 10800: 2.5, 14400: 1.5, 18000: 0.0, 21600: -1.0})
        con.commit()
        self.assertEqual((ins, upd), (1, 1))
        g = lambda ts: con.execute("SELECT kwh_dom, src, n FROM hourly WHERE ts=?", (ts,)).fetchone()
        self.assertEqual(g(7200)[0], 3.0)                      # existujúca hodnota sa neprepíše
        self.assertEqual(g(10800), (2.5, "mix", 60))
        self.assertEqual(g(14400), (1.5, "ha", None))
        self.assertIsNone(g(18000))                            # nula = stuhnutý zdroj, nezapisuje sa
        self.assertEqual(stats.apply_kwh_dom(con, {14400: 1.7, 10800: 2.6}), (0, 0))   # idempotentné
        con.close()

    def test_upsert_never_overwrites_kwh_dom_or_with_null(self):
        stats.upsert_hourly(self.cfg, [{"ts": 3600, "n": 60, "src": "ha", "kwh_dom": 2.0}])
        stats.upsert_hourly(self.cfg, [{"ts": 3600, "n": 60, "src": "tc", "vonku": 4.0, "kwh_dom": 5.0, "kwh": 1.0}])
        r = self.hourly_row(3600)
        self.assertEqual(r["kwh_dom"], 2.0)
        self.assertEqual(r["src"], "mix")
        self.assertEqual(r["kwh"], 1.0)

    def test_refresh_without_ha_is_silent_skip(self):
        r = stats.refresh_kwh_dom(self.cfg)
        self.assertTrue(r["ok"])
        self.assertTrue(r["skipped"])

    def test_refresh_ha_down_returns_error_not_exception(self):
        cfg = dict(self.cfg, HA_URL="http://127.0.0.1:9", HA_TOKEN="x")
        r = stats.refresh_kwh_dom(cfg)
        self.assertFalse(r["ok"])
        self.assertTrue(r["error"])
        self.assertNotIn("HA_TOKEN", r["error"])

    def test_tick_survives_ha_error_and_logs_once(self):
        self.minutes_for_tick()
        cfg = dict(self.cfg, HA_URL="http://127.0.0.1:9", HA_TOKEN="x")
        st = {}
        now = mid(2026, 1, 10) + 2 * HOUR + 120
        r = stats.stats_tick(cfg, st, now=now)
        self.assertTrue(r["ok"], r)
        self.assertTrue(st["dom_err"])
        r = stats.stats_tick(cfg, st, now=now + 300)           # v tej istej hodine sa HA znova nevolá
        self.assertTrue(r["ok"], r)

    def minutes_for_tick(self):
        t0 = mid(2026, 1, 10)
        Env.minutes(self, t0, 130)

    def test_energy_hourly_parsing_and_fallback(self):
        class Fake:
            def __init__(self, res):
                self.res = res

            def call(self, payload):
                self.payload = payload
                return self.res
        B = 1767225600 + 7200        # 2026-01-01 02:00 UTC
        ext = {ha_ws.ENERGY_EXT_ID: [{"start": B * 1000, "change": 1.5}, {"start": (B + 3600) * 1000, "change": 0.0},
                                     {"start": "2026-01-01T04:00:00+00:00", "change": 2.25}, {"start": (B + 4 * 3600) * 1000, "change": None}]}
        d, info = ha_ws.energy_hourly(Fake(ext), B, B + 20000)
        self.assertEqual(d, {B: 1.5, B + 7200: 2.25})
        self.assertEqual(info["zdroj"], "ssd_ims")
        # tarifné počítadlá slúžia len ako záloha a stuhnuté (change 0) nedajú nič
        frozen = {ha_ws.ENERGY_METER_IDS[0]: [{"start": B * 1000, "change": 0.0}], ha_ws.ENERGY_METER_IDS[1]: [{"start": B * 1000, "change": 0.0}]}
        self.assertEqual(ha_ws.energy_hourly(Fake(frozen), B, B + 20000)[0], {})
        live = {ha_ws.ENERGY_METER_IDS[0]: [{"start": B * 1000, "change": 0.5}], ha_ws.ENERGY_METER_IDS[1]: [{"start": B * 1000, "change": 0.25}]}
        d, info = ha_ws.energy_hourly(Fake(live), B, B + 20000)
        self.assertEqual(d, {B: 0.75})
        self.assertEqual(info["zdroj"], "tarify")


class Live(unittest.TestCase, Env):
    def setUp(self):
        self.setUpEnv()

    def tearDown(self):
        self.tearDownEnv()

    def test_live_values(self):
        now = int(time.time())
        now -= now % 60
        t0 = max(now - 3 * 3600, stats._midnight(stats._today()))
        n = (now - t0) // 60
        if n < 40:
            self.skipTest("príliš skoro po polnoci")
        self.minutes(t0, n)                                    # 2,4 kW elektrina, 5,23 kW teplo, COP 2,18
        con = stats.connect(self.cfg)
        stats._meta_set(con, "kalibracia", '{"k": 0.9, "ts": 1}')
        con.commit()
        con.close()
        t = time.time()
        r = stats.live(self.cfg, now=now)
        self.assertLess(time.time() - t, 0.5)
        self.assertAlmostEqual(r["k"], 0.9)
        self.assertEqual(r["kwh_zdroj"], "kalibrovany")
        self.assertAlmostEqual(r["p_el_w"], 2160.0, delta=1)   # 2400 W × 0,9
        self.assertAlmostEqual(r["p_th_kw"], 5.2325, delta=0.02)
        self.assertAlmostEqual(r["cop_now"], 5.2325 / 2.16, delta=0.03)
        self.assertAlmostEqual(r["cop_day"], 5.2325 / 2.16, delta=0.03)
        self.assertAlmostEqual(r["kwh_day"], 0.04 * (n - 1) * 0.9, delta=0.1)
        self.assertIsNone(r["kwh_day_dom"])
        self.assertTrue(r["q_ok"])
        self.assertEqual(r["updated_ts"], t0 + 60 * (n - 1))

    def test_live_compressor_stopped_gives_no_cop_now(self):
        now = int(time.time())
        now -= now % 60
        t0 = max(now - 3600, stats._midnight(stats._today()))
        n = max(5, (now - t0) // 60)
        self.minutes(t0, n, freq=0.0)
        r = stats.live(self.cfg, now=now)
        self.assertIsNone(r["cop_now"])
        self.assertIsNone(r["p_th_kw"])
        self.assertEqual(r["kwh_zdroj"], "odhad")
        self.assertIsNone(r["k"])

    def test_live_invalid_flow(self):
        now = int(time.time())
        now -= now % 60
        t0 = max(now - 3600, stats._midnight(stats._today()))
        self.minutes(t0, max(5, (now - t0) // 60), flow=6553.5)
        r = stats.live(self.cfg, now=now)
        self.assertIsNone(r["cop_now"])
        self.assertIsNone(r["cop_day"])
        self.assertFalse(r["q_ok"])
        self.assertIsNotNone(r["p_el_w"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
