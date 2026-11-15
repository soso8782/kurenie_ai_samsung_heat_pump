#!/usr/bin/env python3
"""Testy v2 (úloha A2): štatistiky, COP a model zariadenia podľa profilu (profil.py).
 - predvolený profil = konštanty identické s verziou 1 (test_profil_v1.json, pôvodné HCOLS/AVG_KEYS, tabuľka výrobcu),
 - iný profil (3 izby, podlahovka, bez TÚV, bez modelu): dynamické stĺpce hourly, migrácia, COP/prietok bez tabuľky, vonku.py, stats_backfill.
Spustenie: python3 test_v2_stats.py   (v priečinku kurenia; používa len dočasné databázy)"""
import copy, importlib, json, os, shutil, sqlite3, sys, tempfile, time, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import profil
import stats
import stats_backfill
import vonku

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "test_profil_v1.json"), encoding="utf-8") as _f:
    V1 = json.load(_f)

V1_HCOLS = ("vonku", "vonku_min", "vonku_max", "ciel", "detska", "jedalen", "kuchyna", "obyvacka", "spalna", "pracovna",
            "kupelna", "chodba", "kotolna", "tw2", "tw1", "wl", "dt_beh", "tuv", "tuv_ciel", "tuv_min",
            "beh_min", "kurenie_min", "odmraz_min", "starty", "hz_avg", "hz_max", "kwh", "prikon",
            "kwh_dom", "kwh_cal", "q_kwh", "cop")
V1_AVG = ("vonku", "ciel", "detska", "jedalen", "kuchyna", "obyvacka", "spalna", "pracovna", "kupelna",
          "tw2", "tw1", "wl", "tuv", "tuv_ciel", "prikon")


def iny_profil(model=""):
    """Iný dom: podlahovka, 3 izby (referenčná obývačka), bez TÚV a bez ventilu/odmrazovania, jedna ďalšia teplota (garáž)."""
    p = profil.default()
    p["zariadenie"].update({"model": model, "nazov": "Samsung EHS Mono 8 kW", "typ": "mono", "vykon_kw": 8.0, "fazy": 1,
                            "chladivo": "R32", "kurenie": "podlahove", "tuv": False})
    e = p["esp"]["entity"]
    e.update({"sledovana": "Sledovaná teplota (Obývačka)", "tuv": "", "tuv_ciel": "", "ventil": "", "odmrazovanie": "",
              "termostat": "", "flow": ""})
    p["izby"] = [
        {"kluc": "obyvacka", "nazov": "Obývačka", "esp": "Izba Obývačka", "referencna": True, "min": None, "ha": ["sensor.ob_t"]},
        {"kluc": "spalna", "nazov": "Spálňa", "esp": "Izba Spálňa", "min": 20.0, "ha": ["sensor.sp_t"]},
        {"kluc": "loznica", "nazov": "Ložnica hostí", "esp": "", "min": 20.0, "ha": []},
    ]
    p["ine_teploty"] = {"garaz": ["sensor.garaz_t"]}
    p["ha"] = {"vonku": ["sensor.vonku_t"], "tw2": ["sensor.tw2"]}
    return p


class DefaultProfile(unittest.TestCase):
    def setUp(self):
        stats.configure(profil.default())

    def test_konstanty_ako_v1(self):
        self.assertEqual(stats.TC, V1["TC"])
        self.assertEqual(list(stats.TC), list(V1["TC"]))            # aj poradie
        self.assertEqual(stats.INV, {v: k for k, v in V1["TC"].items()})
        self.assertEqual((stats.VALVE, stats.DEFROST, stats.THERMOSTAT), (V1["VALVE"], V1["DEFROST"], V1["THERMOSTAT"]))
        self.assertEqual(stats.AGENT_TEMPS, V1["AGENT_TEMPS"])
        self.assertEqual(stats.HCOLS, V1_HCOLS)
        self.assertEqual(stats.AVG_KEYS, V1_AVG)
        self.assertEqual(stats.ROOM_KEYS, ("detska", "jedalen", "kuchyna", "obyvacka", "spalna", "pracovna", "kupelna", "chodba", "kotolna"))
        self.assertEqual(stats.FLOWCAL_ZDROJ, "tabuľky výrobcu (AE120JXED, bez odmrazovania)")
        self.assertEqual([c for c, _ in stats_backfill.COLUMN_IDS], [c for c, _ in V1["COLUMN_IDS"]])
        self.assertEqual([[c, ids] for c, ids in stats_backfill.COLUMN_IDS], V1["COLUMN_IDS"])

    def test_metriky_a_stlpce(self):
        keys = [m["key"] for m in stats.METRICS]
        self.assertEqual(keys, ["vonku", "ciel", "detska", "jedalen", "kuchyna", "obyvacka", "spalna", "pracovna", "kupelna",
                                "chodba", "kotolna", "tw2", "tw1", "wl", "dt_beh", "tuv", "tuv_ciel", "tuv_min", "beh_min",
                                "kurenie_min", "odmraz_min", "starty", "hz_avg", "hz_max", "kwh", "kwh_cal", "kwh_dom", "prikon",
                                "q_kwh", "cop", "n"])
        lab = {m["key"]: m["label"] for m in stats.METRICS}
        self.assertEqual((lab["detska"], lab["jedalen"], lab["chodba"], lab["kotolna"]),
                         ("Detská (riadiaca)", "Jedáleň", "Chodba", "Kotolňa"))
        d = tempfile.mkdtemp()
        try:
            con = stats.connect({"STATS_DB": os.path.join(d, "s.db")})
            cols = [r[1] for r in con.execute("PRAGMA table_info(hourly)")]
            con.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)
        self.assertEqual(set(cols), {"ts", "n", "src", "updated"} | set(V1_HCOLS))

    def test_tabulka_modelu_ako_v1(self):
        self.assertIsNotNone(stats.MODEL)
        self.assertTrue(stats.MODEL["overene"])
        self.assertEqual(stats.TAB_TA, (-20, -15, -10, -7, -2, 2, 7, 10, 15, 20))
        self.assertEqual(stats.TAB_LW, (25, 30, 35, 40, 45, 50, 55))
        self.assertEqual(stats.TAB_PEAK[-20], ((9.84, 3.38), (9.55, 3.91), (9.26, 4.45), (8.96, 4.99), None, None, None))
        self.assertEqual(stats.TAB_PEAK[20][-1], (13.70, 3.96))
        self.assertEqual(sum(len(r) for r in stats.TAB_PEAK.values()), 70)
        self.assertAlmostEqual(stats.cop_tab(7, 35), 12.00 / 2.59, places=6)
        self.assertAlmostEqual(stats.cop_tab(-15, 25), 10.73 / 3.38, places=6)
        self.assertAlmostEqual(stats.cop_tab(-20, 55), 8.96 / 4.99, places=6)       # None v riadku -> posledná platná bunka


class InyProfil(unittest.TestCase):
    def setUp(self):
        self.p = iny_profil()
        self.assertEqual(profil.validate(self.p)[1], [])
        stats.configure(self.p)
        self.dir = tempfile.mkdtemp(prefix="t_v2_stats_")
        self.cfg = {"TC_DB": os.path.join(self.dir, "tc.db"), "STATS_DB": os.path.join(self.dir, "stats.db"),
                    "AGENT_DB": os.path.join(self.dir, "none.db")}
        tc = sqlite3.connect(self.cfg["TC_DB"])
        tc.executescript("CREATE TABLE samples(ts INTEGER, entity TEXT, value REAL); CREATE INDEX samples_ts ON samples(ts);"
                         "CREATE TABLE events(ts INTEGER, kind TEXT, entity TEXT, old TEXT, new TEXT, text TEXT);")
        tc.commit()
        self.tc = tc

    def tearDown(self):
        self.tc.close()
        shutil.rmtree(self.dir, ignore_errors=True)
        stats.configure(profil.default())

    def cols(self, con=None):
        c = con or stats.connect(self.cfg)
        try:
            return [r[1] for r in c.execute("PRAGMA table_info(hourly)")]
        finally:
            if con is None:
                c.close()

    def test_mapy_a_stlpce(self):
        self.assertEqual(stats.ROOM_KEYS, ("obyvacka", "spalna", "loznica", "garaz"))
        self.assertEqual(stats.HCOLS[:8], ("vonku", "vonku_min", "vonku_max", "ciel", "obyvacka", "spalna", "loznica", "garaz"))
        self.assertNotIn("detska", stats.HCOLS)
        self.assertIn("obyvacka", stats.AVG_KEYS)
        self.assertNotIn("garaz", stats.AVG_KEYS)               # ďalšie teploty prichádzajú z agent.db, nie z tc.db
        self.assertNotIn("tuv", stats.TC)
        self.assertEqual((stats.VALVE, stats.DEFROST, stats.THERMOSTAT), ("", "", ""))
        self.assertEqual(stats.AGENT_TEMPS, {"garaz": "sensor.garaz_t"})
        keys = [m["key"] for m in stats.METRICS]
        self.assertEqual(keys[:8], ["vonku", "ciel", "obyvacka", "spalna", "loznica", "garaz", "tw2", "tw1"])
        lab = {m["key"]: m["label"] for m in stats.METRICS}
        self.assertEqual((lab["obyvacka"], lab["spalna"], lab["garaz"]), ("Obývačka (riadiaca)", "Spálňa", "Garaz"))
        self.assertEqual(stats._MDEC["obyvacka"], 1)
        self.assertEqual(self.cols(), ["ts", "n", "src"] + list(stats.HCOLS) + ["updated"])

    def test_migracia_pridava_stlpce_a_nemaze(self):
        stats.configure(profil.default())
        con = stats.connect(self.cfg)             # databáza z verzie 1
        con.execute("INSERT INTO hourly(ts, n, src, detska, vonku) VALUES(3600, 5, 'tc', 21.5, 3.0)")
        con.commit()
        con.close()
        stats.configure(self.p)
        con = stats.connect(self.cfg)             # nový profil: pribudne loznica, detska ostáva
        try:
            cols = self.cols(con)
            self.assertIn("loznica", cols)
            self.assertIn("garaz", cols)
            self.assertIn("detska", cols)
            self.assertEqual(con.execute("SELECT detska, vonku FROM hourly WHERE ts=3600").fetchone(), (21.5, 3.0))
        finally:
            con.close()
        stats.upsert_hourly(self.cfg, [{"ts": 7200, "vonku": 1.0, "loznica": 19.5, "garaz": 8.0}])
        con = stats.connect(self.cfg)
        try:
            self.assertEqual(con.execute("SELECT loznica, garaz FROM hourly WHERE ts=7200").fetchone(), (19.5, 8.0))
        finally:
            con.close()
        self.assertEqual(stats.coverage(self.cfg)["hours"], 2)

    def _hodina(self, flow_sensor=None, flow_entity=False):
        """60 minút behu kompresora v jednej hodine: Tw2 40, Tw1 35 (ΔT 5 K), počítadlo kWh rastie 0,04 za minútu."""
        a = int(time.mktime((2026, 1, 10, 10, 0, 0, 0, 0, -1)))
        T = stats.TC
        rows = []
        kw = 0.0
        for i in range(60):
            ts = a + 60 * i
            kw += 0.04
            rows += [(ts, T["vonku"], 2.0), (ts, T["freq"], 40.0), (ts, T["tw2"], 40.0), (ts, T["tw1"], 35.0),
                     (ts, T["kwh"], kw), (ts, T["prikon"], 2400.0), (ts, T["obyvacka"], 21.0)]
            if flow_sensor is not None and "flow" in T:
                rows.append((ts, T["flow"], flow_sensor))
        self.tc.executemany("INSERT INTO samples VALUES(?,?,?)", rows)
        self.tc.commit()
        return a

    def _agreguj(self, a, cfg=None):
        cfg = cfg or self.cfg
        self.assertEqual(stats.aggregate_range(cfg, a, a + 3600), 1)
        con = stats.connect(cfg)
        try:
            con.row_factory = sqlite3.Row
            return dict(con.execute("SELECT * FROM hourly WHERE ts=?", (a,)).fetchone())
        finally:
            con.close()

    def test_agregacia_bez_modelu_a_bez_prietoku(self):
        self.assertIsNone(stats.MODEL)
        self.assertEqual((stats.TAB_TA, stats.TAB_PEAK), ((), {}))
        self.assertIsNone(stats.cop_tab(2.0, 40.0))
        a = self._hodina()
        r = self._agreguj(a)
        self.assertEqual(r["obyvacka"], 21.0)
        self.assertIsNone(r["spalna"])
        self.assertAlmostEqual(r["kwh"], 2.36, places=2)       # 59 prírastkov (prvá minúta nemá predchádzajúcu hodnotu)
        self.assertEqual(r["beh_min"], 60.0)
        self.assertIsNone(r["q_kwh"])                         # bez modelu a bez prietoku: žiadne teplo ani COP, nie pád
        self.assertIsNone(r["cop"])

    def test_prietok_zo_senzora_alebo_konfiguracie(self):
        stats.configure(self._s_prietokom())
        a = self._hodina(flow_sensor=15.0)
        r = self._agreguj(a)
        q = (40.0 - 35.0) * 15.0 / 60.0 * stats.CP_WATER      # kW = kWh za hodinu pri plnom behu
        self.assertAlmostEqual(r["q_kwh"], q, delta=0.02)
        self.assertAlmostEqual(r["cop"], q / r["kwh"], delta=0.02)
        self.tc.execute("DELETE FROM samples")
        self.tc.commit()
        con = stats.connect(self.cfg)
        con.execute("DELETE FROM hourly")
        con.commit()
        con.close()
        stats.configure(self.p)                               # bez senzora prietoku: pevný prietok z konfigurácie
        a = self._hodina()
        r = self._agreguj(a, dict(self.cfg, STATS_FLOW_LPM="15"))
        self.assertAlmostEqual(r["q_kwh"], q, delta=0.02)

    def _s_prietokom(self):
        p = copy.deepcopy(self.p)
        p["esp"]["entity"]["flow"] = "Prietok vody"
        return p

    def test_kalibracia_prietoku_bez_modelu_sa_preskoci(self):
        res = stats.flow_calibrate(self.cfg, now=int(time.time()))
        self.assertNotIn("error", res)
        self.assertIsNone(res["kurenie"])
        self.assertIsNone(res["tuv"])
        self.assertEqual(stats.get_flows(stats.connect(self.cfg)), {"room": None, "tank": None})
        self.assertIn("bez tabuľky", stats.FLOWCAL_ZDROJ)

    def test_flow_calc_bez_modelu(self):
        ts = range(0, 60 * 40, 60)                            # 40 minút nepretržitého behu, ΔT 5 K
        smp = {"freq": {t: 40.0 for t in ts}, "tw2": {t: 40.0 for t in ts}, "tw1": {t: 35.0 for t in ts},
               "prikon": {t: 2400.0 for t in ts}, "vonku": {t: 2.0 for t in ts}}
        res = stats.flow_calc(smp, stats._Track("room", []), [], None)
        self.assertEqual((res["kurenie"], res["tuv"], res["n_kurenie"], res["n_tuv"]), (None, None, 0, 0))

    def test_ostatne_funkcie_bez_entit(self):
        a = self._hodina()
        self._agreguj(a)
        self.assertEqual(stats.compute_cycles(self.cfg, a - 3600, a + 7200)[0]["minutes"] > 0, True)
        cv = stats.coverage(self.cfg)
        self.assertEqual(cv["hours"], 1)
        self.assertIsNotNone(cv["first_day"]["izby"])         # stĺpce izieb podľa profilu


class ModulyPodlaProfilu(unittest.TestCase):
    """vonku.py a stats_backfill.py si mapy načítajú pri importe: profil sa podvrhne cez profil.load a moduly sa znova načítajú."""

    def setUp(self):
        self._load = profil.load
        p = iny_profil()
        profil.load = lambda path=None: copy.deepcopy(p)

    def tearDown(self):
        profil.load = self._load
        importlib.reload(vonku)
        importlib.reload(stats_backfill)

    def test_vonku(self):
        v = importlib.reload(vonku)
        self.assertEqual(v.IZBY, ["obyvacka", "spalna", "loznica", "garaz"])
        self.assertEqual(v.NAZVY["spalna"], "Spálňa")
        self.assertEqual(v.NAZVY["loznica"], "Ložnica hostí")
        self.assertEqual(v.NAZVY["garaz"], "Garaz")
        self.assertEqual(v.E_VONKU, "Vonkajšia teplota")
        self.assertEqual(dict(v.NAST)["fsv2031"], "FSV 2031 WL2 voda pri najchladnejšom")
        d = tempfile.mkdtemp()
        try:
            stats.configure(iny_profil())
            cfg = {"STATS_DB": os.path.join(d, "s.db"), "TC_DB": os.path.join(d, "tc.db")}
            A = int(time.mktime((2026, 1, 10, 0, 0, 0, 0, 0, -1)))
            stats.upsert_hourly(cfg, [{"ts": A + 3600 * i, "vonku": 1.0, "obyvacka": 22.0, "garaz": 9.0 + i} for i in range(4)])
            r = v.report(cfg, "2026-01-10", "2026-01-10", 2)
            self.assertEqual([(s["key"], s["skupina"]) for s in r["stlpce"]], [("obyvacka", "izba"), ("garaz", "izba")])
            self.assertEqual(r["stlpce"][0]["name"], "Obývačka")
        finally:
            stats.configure(profil.default())
            shutil.rmtree(d, ignore_errors=True)

    def test_backfill(self):
        b = importlib.reload(stats_backfill)
        self.assertEqual([c for c, _ in b.COLUMN_IDS], ["vonku", "obyvacka", "spalna", "garaz", "tw2"])
        self.assertEqual(b.COLUMN_IDS[1], ("obyvacka", ["sensor.ob_t"]))
        self.assertEqual(b.VALUE_COLS, ["vonku", "obyvacka", "spalna", "garaz", "tw2", "vonku_min", "vonku_max", "kwh_dom"])
        self.assertEqual(b.ALL_IDS, ["sensor.vonku_t", "sensor.ob_t", "sensor.sp_t", "sensor.garaz_t", "sensor.tw2"])
        con = sqlite3.connect(":memory:")
        con.executescript(b.HOURLY_DDL + ";" + b.META_DDL + ";")
        b.migrate(con)
        b.write_hours(con, {3600: {"obyvacka": 21.0, "garaz": 7.0}}, 1)
        self.assertEqual(con.execute("SELECT obyvacka, garaz FROM hourly").fetchone(), (21.0, 7.0))


if __name__ == "__main__":
    unittest.main()
