#!/usr/bin/env python3
"""Testy SPEC_cop: tabuľka výrobcu (cop_tab), nočná kalibrácia k, prietok okruhov (flow_calc, flow_for), záporné teplo pri
odmrazovaní a použitie dvoch prietokov v hodinovej agregácii. Syntetické dáta, dočasné DB.
Spustenie: python test_cop.py   (v priečinku kurenia; nič nezapisuje do ostrých databáz)"""
import os, sys, time, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stats
from t_energia import Env, synth_rows, mid

HOUR = 3600
CP = stats.CP_WATER


class CopTab(unittest.TestCase):
    def test_control_points(self):
        for (ta, lw), exp in (((7, 35), 4.63), ((2, 35), 3.70), ((0, 37), 3.29), ((0, 47), 2.68)):
            self.assertAlmostEqual(stats.cop_tab(ta, lw), exp, delta=0.02, msg=(ta, lw))

    def test_grid_point_is_hc_over_pi(self):
        self.assertAlmostEqual(stats.cop_tab(7, 35), 12.00 / 2.59, places=6)
        self.assertAlmostEqual(stats.cop_tab(-15, 25), 10.73 / 3.38, places=6)

    def test_clipping_outside_range(self):
        self.assertAlmostEqual(stats.cop_tab(40, 20), stats.cop_tab(20, 25), places=9)
        self.assertAlmostEqual(stats.cop_tab(-40, 30), stats.cop_tab(-20, 30), places=9)
        self.assertAlmostEqual(stats.cop_tab(7, 90), stats.cop_tab(7, 55), places=9)

    def test_none_cells_use_last_valid_in_row(self):
        # -20 °C: od 45 °C vyššie je bunka None -> platí hodnota pri 40 °C (8,96 / 4,99)
        for lw in (45, 50, 55):
            self.assertAlmostEqual(stats.cop_tab(-20, lw), 8.96 / 4.99, places=6)
        self.assertAlmostEqual(stats.cop_tab(-15, 55), 9.63 / 5.53, places=6)
        self.assertGreater(stats.cop_tab(-17, 50), 1.0)             # interpolácia cez riadky s None nevyhodí výnimku


class _K:
    def __init__(self, base=2.0):
        self.base = base


def night_rows(days=10, k=1.02, b0=0.1, start_day=5):
    """Nočné hodiny 0–4 presne dom = b0 + k·kwh, denné hodiny so silným šumom spotreby domu."""
    rows = []
    for d in range(days):
        day0 = mid(2026, 1, start_day + d)
        for h in range(24):
            ts = day0 + h * HOUR
            if h < 5:
                kwh = 0.4 + 0.15 * ((d * 5 + h) % 9)
                dom = b0 + k * kwh
            else:
                kwh = 0.5 + 0.1 * (h % 6)
                dom = b0 + k * kwh + 0.4 + 0.5 * ((h * 7 + d) % 5)           # šum: varenie, auto, ...
            rows.append((ts, 60.0, kwh, dom))
    return rows


class Calibration(unittest.TestCase):
    NOW = mid(2026, 1, 20)

    def test_night_method(self):
        res = stats.calibration_calc(night_rows(), self.NOW)
        self.assertEqual(res["metoda"], "noc")
        self.assertAlmostEqual(res["k"], 1.02, delta=0.01)
        self.assertAlmostEqual(res["k_raw"], 1.02, delta=0.01)
        self.assertAlmostEqual(res["noc_zaklad_kwh"], 0.1, delta=0.02)
        self.assertEqual(res["n_hodin"], 50)
        self.assertEqual(res["n_dni"], 10)
        self.assertLess(res["mape"], 1.0)
        for key in ("base_kwh", "base_zdroj", "base_hodin", "zdroj", "dovod", "ts"):
            self.assertIn(key, res)

    def test_night_method_robust_to_outlier_hours(self):
        rows = night_rows()
        rows = [(ts, b, kwh, dom + (3.0 if i % 17 == 0 and time.localtime(ts).tm_hour < 5 else 0.0))
                for i, (ts, b, kwh, dom) in enumerate(rows)]
        res = stats.calibration_calc(rows, self.NOW)
        self.assertEqual(res["metoda"], "noc")
        self.assertAlmostEqual(res["k"], 1.02, delta=0.03)

    def test_night_method_clamped(self):
        res = stats.calibration_calc(night_rows(k=2.2), self.NOW)
        self.assertEqual(res["metoda"], "noc")
        self.assertEqual(res["k"], stats.K_HI)
        self.assertGreater(res["k_raw"], 2.0)
        self.assertTrue(res["dovod"])

    def test_night_method_returned_even_without_base(self):
        # nepretržitý chod aj v noci a základ sa nedá určiť inak -> k z nočnej metódy sa aj tak vráti
        res = stats.calibration_calc(night_rows(), self.NOW)
        self.assertIsNotNone(res["k"])

    def test_few_night_hours_fall_back_to_days(self):
        # hodiny 0–4 iba v 2 dňoch (10 hodín, < 12 a < 3 dni) -> metóda dní
        rows = synth_rows(8, 0.9, lambda h: 0.3, idle_hours=set(range(0, 8)))
        res = stats.calibration_calc(rows, self.NOW)
        self.assertEqual(res["metoda"], "dni")
        self.assertIsNone(res["noc_zaklad_kwh"])
        self.assertAlmostEqual(res["k"], 0.9, delta=0.02)
        self.assertEqual(res["n_dni"], 8)
        self.assertAlmostEqual(res["base_kwh"], 0.3, places=3)

    def test_small_span_or_too_few_days_fall_back(self):
        rows = [r for r in night_rows(days=2) if time.localtime(r[0]).tm_hour < 5 or True]
        self.assertEqual(stats.calibration_calc(rows, self.NOW)["metoda"], "dni")      # 10 nočných hodín < 12
        # dosť hodín, ale kwh takmer rovnaké (rozpätie < 0,8)
        rows = [(ts, b, (1.0 + 0.05 * (i % 3)) if time.localtime(ts).tm_hour < 5 else kwh, dom)
                for i, (ts, b, kwh, dom) in enumerate(night_rows())]
        self.assertEqual(stats.calibration_calc(rows, self.NOW)["metoda"], "dni")

    def test_night_filters(self):
        # nočné hodiny s krátkym behom (< 20 min) alebo malou spotrebou (< 0,3 kWh) sa nepoužijú
        rows = [(ts, 10.0 if time.localtime(ts).tm_hour < 5 else b, kwh, dom) for ts, b, kwh, dom in night_rows()]
        self.assertEqual(stats.calibration_calc(rows, self.NOW)["metoda"], "dni")

    def test_theil_sen_helper(self):
        pts = [(x * 0.5, 0.2 + 1.1 * x * 0.5) for x in range(10)]
        b1, b0 = stats._theil_sen(pts)
        self.assertAlmostEqual(b1, 1.1, places=6)
        self.assertAlmostEqual(b0, 0.2, places=6)
        self.assertAlmostEqual(stats._ts_intercept(pts), 0.2, places=6)
        self.assertIsNone(stats._theil_sen([(1.0, 1.0), (1.01, 1.2), (1.02, 1.0)]))


def synth_run(room_min=400, tank_min=120, flow_room=25.0, flow_tank=40.0, defr_at=None, prikon_scale=1.0):
    """Nepretržitý chod kompresora: room_min minút kúrenia, potom tank_min minút TÚV. Príkon je dopočítaný cez cop_tab tak, aby
    flow_calc vrátil práve flow_*. Vracia (samples, valve, t0)."""
    t0 = 1_800_000_000 - 1_800_000_000 % 60
    s = {k: {} for k in ("freq", "tw2", "tw1", "prikon", "vonku")}
    for i in range(room_min + tank_min):
        t = t0 + 60 * i
        tank = i >= room_min
        tw2, tw1, fl = (50.0, 45.0, flow_tank) if tank else (38.0, 33.0, flow_room)
        heat = fl / 60.0 * CP * 5.0
        s["freq"][t] = 50.0
        s["tw2"][t], s["tw1"][t] = tw2, tw1
        s["vonku"][t] = 2.0
        s["prikon"][t] = heat * 1000.0 / stats.cop_tab(2.0, tw2) / prikon_scale
    valve = stats._Track("room", [(t0 + 60 * room_min, "tank")])
    return s, valve, t0


class FlowCalc(unittest.TestCase):
    def test_recovers_both_circuits(self):
        s, valve, _ = synth_run()
        r = stats.flow_calc(s, valve, [], None)
        self.assertAlmostEqual(r["kurenie"], 25.0, delta=0.2)
        self.assertAlmostEqual(r["tuv"], 40.0, delta=0.2)
        self.assertEqual(r["n_kurenie"], 385)                # prvých 15 min po štarte sa nepoužije
        self.assertEqual(r["n_tuv"], 110)                    # prvých 10 min po zmene ventilu sa nepoužije
        for key in ("p25_kurenie", "p75_kurenie", "p25_tuv", "p75_tuv"):
            self.assertIn(key, r)
        self.assertAlmostEqual(r["p25_kurenie"], 25.0, delta=0.2)

    def test_k_scales_result(self):
        s, valve, _ = synth_run()
        a = stats.flow_calc(s, valve, [], None)
        b = stats.flow_calc(s, valve, [], 1.0)
        c = stats.flow_calc(s, valve, [], 0.5)
        self.assertEqual(a["kurenie"], b["kurenie"])
        self.assertAlmostEqual(c["kurenie"], 12.5, delta=0.2)

    def test_defrost_and_restart_minutes_ignored(self):
        s, valve, t0 = synth_run()
        d0, d1 = t0 + 200 * 60, t0 + 205 * 60
        for i in range(200, 206):                            # odmrazovanie: kompresor stojí
            s["freq"][t0 + 60 * i] = 0.0
        for i in range(206, 221):                            # po odmrazovaní zaujaté hodnoty (nesmú sa použiť)
            s["tw1"][t0 + 60 * i] = 37.0
        for i in range(190, 200):                            # a tesne pred ním
            s["tw1"][t0 + 60 * i] = 37.0
        r = stats.flow_calc(s, valve, [(d0, d1)], None)
        self.assertEqual(r["n_kurenie"], 385 - 31)           # minúty 190..220 vypadnú
        self.assertAlmostEqual(r["kurenie"], 25.0, delta=0.2)
        self.assertEqual(r["n_tuv"], 110)

    def test_missing_sample_counts_as_not_running(self):
        s, valve, t0 = synth_run()
        for k in s:
            del s[k][t0 + 60 * 100]
        # chýbajúca minúta 100: nepoužije sa ona a ďalších 15 minút (stabilizácia)
        r = stats.flow_calc(s, valve, [], None)
        self.assertEqual(r["n_kurenie"], 385 - 16)

    def test_few_minutes_give_none(self):
        s, valve, _ = synth_run(room_min=100, tank_min=30)
        r = stats.flow_calc(s, valve, [], None)
        self.assertIsNone(r["kurenie"])
        self.assertIsNone(r["tuv"])
        self.assertEqual(r["n_kurenie"], 85)
        self.assertEqual(r["n_tuv"], 20)

    def test_out_of_range_gives_none(self):
        s, valve, _ = synth_run(flow_room=70.0, flow_tank=5.0)
        r = stats.flow_calc(s, valve, [], None)
        self.assertIsNone(r["kurenie"])
        self.assertIsNone(r["tuv"])

    def test_low_delta_t_and_low_power_ignored(self):
        s, valve, t0 = synth_run()
        for i in range(0, 400):
            s["tw1"][t0 + 60 * i] = s["tw2"][t0 + 60 * i] - 1.0         # ΔT < 1,5
        r = stats.flow_calc(s, valve, [], None)
        self.assertEqual(r["n_kurenie"], 0)
        self.assertIsNone(r["kurenie"])
        s, valve, t0 = synth_run()
        for i in range(0, 400):
            s["prikon"][t0 + 60 * i] = 250.0                            # príkon < 300 W
        self.assertEqual(stats.flow_calc(s, valve, [], None)["n_kurenie"], 0)

    def test_unknown_valve_not_used(self):
        s, _, _ = synth_run()
        r = stats.flow_calc(s, stats._Track(None, []), [], None)
        self.assertEqual((r["n_kurenie"], r["n_tuv"]), (0, 0))


class FlowFor(unittest.TestCase):
    CAL = {"room": 22.5, "tank": 42.0}

    def test_priority(self):
        cfg = {"room": 20.0, "tank": 38.0}
        self.assertEqual(stats.flow_for("room", 18.0, cfg, self.CAL), (18.0, "senzor"))
        self.assertEqual(stats.flow_for("tank", 18.0, cfg, self.CAL), (18.0, "senzor"))
        self.assertEqual(stats.flow_for("room", 6553.5, cfg, self.CAL), (20.0, "zmerany"))
        self.assertEqual(stats.flow_for("tank", None, cfg, self.CAL), (38.0, "zmerany"))
        self.assertEqual(stats.flow_for("room", None, {"room": None, "tank": None}, self.CAL), (22.5, "tabulky"))
        self.assertEqual(stats.flow_for("tank", None, None, self.CAL), (42.0, "tabulky"))
        self.assertEqual(stats.flow_for("room", None, None, None), (None, None))
        self.assertEqual(stats.flow_for("tank", None, {"room": 20.0, "tank": None}, None), (None, None))

    def test_only_room_setting_does_not_apply_to_tank(self):
        cfg = stats.cfg_flows({"STATS_FLOW_LPM": "20"})
        self.assertEqual(cfg, {"room": 20.0, "tank": None})
        self.assertEqual(stats.flow_for("tank", None, cfg, self.CAL), (42.0, "tabulky"))
        self.assertEqual(stats.flow_for("room", None, cfg, self.CAL), (20.0, "zmerany"))

    def test_unknown_valve_is_heating(self):
        self.assertEqual(stats.flow_for(None, None, None, self.CAL), (22.5, "tabulky"))

    def test_cfg_flows_and_wrapper(self):
        c = {"STATS_FLOW_LPM": "21.5", "STATS_FLOW_TUV_LPM": "40"}
        self.assertEqual(stats.cfg_flows(c), {"room": 21.5, "tank": 40.0})
        self.assertEqual(stats.cfg_flow(c), 21.5)
        self.assertIsNone(stats.cfg_flow({}))
        self.assertEqual(stats.cfg_flows({"STATS_FLOW_LPM": "abc", "STATS_FLOW_TUV_LPM": "9999"}), {"room": None, "tank": None})


class Heat(unittest.TestCase):
    def test_pth_negative_for_negative_delta_t(self):
        self.assertLess(stats.pth_kw(20.0, 30.0, 35.0), 0)
        self.assertAlmostEqual(stats.pth_kw(20.0, 30.0, 35.0), -20 / 60.0 * CP * 5, places=6)
        self.assertAlmostEqual(stats.pth_kw(15.0, 40.0, 35.0), 15 / 60.0 * CP * 5, places=6)


class Agg(unittest.TestCase, Env):
    def setUp(self):
        self.setUpEnv()
        self.t0 = mid(2026, 1, 10)

    def tearDown(self):
        self.tearDownEnv()

    def chunk(self, a, b, kcal=None, flows=None, cfg=None):
        tc = stats._tc(cfg or self.cfg)
        try:
            return stats._agg_chunk(cfg or self.cfg, tc, a, b, a + 7200, kcal, flows)
        finally:
            tc.close()

    def test_hour_with_two_circuits_uses_two_flows(self):
        t0 = self.t0
        self.tc.executemany("INSERT INTO events VALUES(?,?,?,?,?,?)", [
            (t0 - 60, "ovladanie", stats.VALVE, "", "Room (Heating)", ""),
            (t0 + 1800, "ovladanie", stats.VALVE, "", "Tank (DHW)", "")])
        self.minutes(t0, 60, flow=None)                       # bez senzora prietoku; ΔT 5 K
        rows = self.chunk(t0, t0 + HOUR, flows={"room": 20.0, "tank": 40.0})
        q_exp = (20.0 + 40.0) / 60.0 * CP * 5.0 * 0.5          # 30 min kúrenia + 30 min TÚV
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["q_kwh"], q_exp, delta=0.02)
        self.assertAlmostEqual(rows[0]["tuv_min"], 30.0, delta=0.5)
        # jeden pevný prietok pre oba okruhy by dal inú hodnotu
        self.assertNotAlmostEqual(rows[0]["q_kwh"], 2 * 20.0 / 60.0 * CP * 5.0 * 0.5, delta=0.5)
        # zmeraný prietok kúrenia z config.env má prednosť pred tabuľkovým, TÚV ostáva tabuľkový
        cfg = dict(self.cfg, STATS_FLOW_LPM="25")
        r2 = self.chunk(t0, t0 + HOUR, flows={"room": 20.0, "tank": 40.0}, cfg=cfg)[0]
        self.assertAlmostEqual(r2["q_kwh"], (25.0 + 40.0) / 60.0 * CP * 5.0 * 0.5, delta=0.02)
        cfg = dict(cfg, STATS_FLOW_TUV_LPM="30")
        r3 = self.chunk(t0, t0 + HOUR, flows={"room": 20.0, "tank": 40.0}, cfg=cfg)[0]
        self.assertAlmostEqual(r3["q_kwh"], (25.0 + 30.0) / 60.0 * CP * 5.0 * 0.5, delta=0.02)
        # senzor má prednosť pred všetkým
        self.tc.execute("DELETE FROM samples")
        self.minutes(t0, 60, flow=10.0)
        r4 = self.chunk(t0, t0 + HOUR, flows={"room": 20.0, "tank": 40.0}, cfg=cfg)[0]
        self.assertAlmostEqual(r4["q_kwh"], 10.0 / 60.0 * CP * 5.0, delta=0.02)

    def test_no_flows_no_heat(self):
        self.minutes(self.t0, 60, flow=None)
        self.assertNotIn("q_kwh", self.chunk(self.t0, self.t0 + HOUR)[0])

    def test_hourly_q_never_negative(self):
        self.minutes(self.t0, 60, tw2=30.0, tw1=35.0)          # ΔT záporné počas celej hodiny (odmrazovanie)
        r = self.chunk(self.t0, self.t0 + HOUR)[0]
        self.assertEqual(r["q_kwh"], 0.0)
        self.assertEqual(r["cop"], 0.0)

    def test_defrost_heat_is_subtracted(self):
        # 50 min kúrenia (+5 K) a 10 min odmrazovania (−5 K) rovnakým prietokom: teplo = 40 min čistého
        self.minutes(self.t0, 50)
        self.minutes(self.t0 + 3000, 10, tw2=30.0, tw1=35.0, kwh0=2.0)
        r = self.chunk(self.t0, self.t0 + HOUR)[0]
        self.assertAlmostEqual(r["q_kwh"], 15.0 / 60.0 * CP * 5.0 * 40 / 60.0, delta=0.02)

    def test_live_negative_heat_gives_no_cop_now(self):
        now = int(time.time())
        now -= now % 60
        t0 = max(now - 3600, stats._midnight(stats._today()))
        n = max(5, (now - t0) // 60)
        self.minutes(t0, n, tw2=30.0, tw1=35.0)
        r = stats.live(self.cfg, now=now)
        self.assertIsNone(r["cop_now"])
        self.assertIsNone(r["p_th_kw"])
        self.assertEqual(r["flow_zdroj"], "senzor")
        self.assertEqual(r["flow_lpm"], 15.0)

    def test_live_flow_source_tables_and_valve(self):
        now = int(time.time())
        now -= now % 60
        t0 = max(now - 3600, stats._midnight(stats._today()))
        n = max(5, (now - t0) // 60)
        self.tc.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (t0 - 60, "ovladanie", stats.VALVE, "", "Tank (DHW)", ""))
        self.tc.commit()
        self.minutes(t0, n, flow=None)
        con = stats.connect(self.cfg)
        stats._meta_set(con, "prietok", '{"kurenie": 22.5, "tuv": 42.0}')
        con.commit()
        con.close()
        r = stats.live(self.cfg, now=now)
        self.assertEqual((r["flow_lpm"], r["flow_zdroj"]), (42.0, "tabulky"))
        self.assertAlmostEqual(r["p_th_kw"], 42.0 / 60.0 * CP * 5.0, delta=0.02)
        self.assertGreater(r["cop_now"], 0)


class FlowCalibrate(unittest.TestCase, Env):
    def setUp(self):
        self.setUpEnv()
        self.t0 = mid(2026, 1, 10)

    def tearDown(self):
        self.tearDownEnv()

    def expected_room(self):
        return stats.cop_tab(0.0, 40.0) * 2.4 / (CP * 5.0 / 60.0)

    def test_flow_calibrate_writes_meta_and_get_flows(self):
        self.tc.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (self.t0 - 60, "ovladanie", stats.VALVE, "", "Room (Heating)", ""))
        self.tc.commit()
        self.minutes(self.t0, 400)
        now = self.t0 + 401 * 60
        res = stats.flow_calibrate(self.cfg, now)
        self.assertNotIn("error", res)
        self.assertAlmostEqual(res["kurenie"], self.expected_room(), delta=0.1)
        self.assertIsNone(res["tuv"])
        self.assertEqual(res["ts"], now)
        self.assertEqual(res["zdroj"], "tabuľky výrobcu (AE120JXED, bez odmrazovania)")
        con = stats.connect(self.cfg)
        fl = stats.get_flows(con)
        self.assertAlmostEqual(fl["room"], self.expected_room(), delta=0.1)
        self.assertIsNone(fl["tank"])
        self.assertEqual(stats.get_prietok(con)["kurenie"], res["kurenie"])
        con.close()

    def test_flow_calibrate_never_raises(self):
        res = stats.flow_calibrate({"TC_DB": os.path.join(self.dir, "neexistuje", "x.db"), "STATS_DB": self.cfg["STATS_DB"]}, 1000)
        self.assertIn("error", res)
        con = stats.connect(self.cfg)
        self.assertIsNone(stats.get_prietok(con))
        self.assertEqual(stats.get_flows(con), {"room": None, "tank": None})
        con.close()

    def test_get_flows_rejects_nonsense(self):
        con = stats.connect(self.cfg)
        stats._meta_set(con, "prietok", '{"kurenie": 6553.5, "tuv": "x"}')
        self.assertEqual(stats.get_flows(con), {"room": None, "tank": None})
        stats._meta_set(con, "prietok", "nie je json")
        self.assertEqual(stats.get_flows(con), {"room": None, "tank": None})
        con.close()

    def test_recalibrate_recomputes_hours_keeps_kwh_dom(self):
        self.tc.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (self.t0 - 60, "ovladanie", stats.VALVE, "", "Room (Heating)", ""))
        self.tc.commit()
        self.minutes(self.t0, 400, flow=None)
        con = stats.connect(self.cfg)
        con.execute("INSERT INTO hourly(ts, n, src, kwh_dom, updated) VALUES(?, 60, 'tc', 1.5, 1)", (self.t0 + HOUR,))
        con.commit()
        con.close()
        now = self.t0 + 401 * 60
        res = stats.recalibrate(self.cfg, now)
        self.assertIn("prietok", res)
        self.assertAlmostEqual(res["prietok"]["kurenie"], self.expected_room(), delta=0.1)
        self.assertGreater(res.get("prepocet_hodin", 0), 0)
        self.assertNotIn("prepocet_chyba", res)
        r = self.hourly_row(self.t0 + HOUR)
        self.assertEqual(r["kwh_dom"], 1.5)
        self.assertAlmostEqual(r["q_kwh"], 2.4 * stats.cop_tab(0.0, 40.0), delta=0.05)
        con = stats.connect(self.cfg)
        self.assertNotIn("prietok", stats.get_cal(con))        # v JSON `kalibracia` sa prietok neukladá
        con.close()
        # ďalší beh bez zmeny nič neprepočítava
        res2 = stats.recalibrate(self.cfg, now)
        self.assertNotIn("prepocet_hodin", res2)

    def test_coverage_and_day_carry_prietok(self):
        self.minutes(self.t0, 60)
        stats.aggregate_range(self.cfg, self.t0, self.t0 + HOUR)
        self.assertIsNone(stats.coverage(self.cfg)["prietok"])
        self.assertIsNone(stats.get_day(self.cfg, "2026-01-10")["prietok"])
        con = stats.connect(self.cfg)
        stats._meta_set(con, "prietok", '{"kurenie": 22.5, "tuv": 42.0}')
        con.commit()
        con.close()
        self.assertEqual(stats.coverage(self.cfg)["prietok"]["tuv"], 42.0)
        self.assertEqual(stats.coverage(self.cfg)["kalibracia"]["prietok"]["kurenie"], 22.5)
        self.assertEqual(stats.get_day(self.cfg, "2026-01-10")["prietok"]["kurenie"], 22.5)


if __name__ == "__main__":
    unittest.main(verbosity=1)
