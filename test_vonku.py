#!/usr/bin/env python3
"""Testy SPEC_vonku: vonku.report (pásmovanie, min/max/avg + časy, vynechanie pásiem, stĺpce, chyby, chýbajúca DB).
Dočasná stats.db. Spustenie: python test_vonku.py   (v priečinku kurenia)"""
import datetime as dt, os, sys, tempfile, time, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stats, vonku

NOW = int(dt.datetime(2026, 11, 14, 12, 0).timestamp())
A = stats._midnight(dt.date(2026, 11, 3))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {"STATS_DB": os.path.join(self.tmp.name, "stats.db")}

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, rows):
        stats.upsert_hourly(self.cfg, rows)

    def rep(self, **kw):
        kw.setdefault("od", "2026-11-03")
        kw.setdefault("do", "2026-11-05")
        return vonku.report(self.cfg, now=NOW, **kw)


class Pasma(Base):
    def test_zaporne_pasmo(self):
        self.put([{"ts": A + 3600 * i, "vonku": v, "obyvacka": 22.0} for i, v in enumerate([-0.5, -1.9, -0.1, 0.0])])
        r = self.rep()
        self.assertEqual([(p["od_c"], p["do_c"], p["hodin"]) for p in r["pasma"]], [(-2, 0, 3)])   # 0,0 patrí do [0, 2) -> len 1 hodina, vynechané
        self.assertEqual(r["hodin_spolu"], 3)

    def test_zoradenie_a_vynechanie_malych(self):
        rows = []
        for i in range(3):
            rows.append({"ts": A + 3600 * i, "vonku": 5.5, "obyvacka": 22.0})
        for i in range(3, 5):
            rows.append({"ts": A + 3600 * i, "vonku": -7.0, "obyvacka": 21.0})     # len 2 hodiny -> vynechané
        for i in range(5, 8):
            rows.append({"ts": A + 3600 * i, "vonku": -3.0, "obyvacka": 20.0})
        self.put(rows)
        r = self.rep(krok=5)
        self.assertEqual([p["od_c"] for p in r["pasma"]], [-5, 5])
        self.assertEqual(r["hodin_spolu"], 6)

    def test_min_max_avg_a_casy(self):
        vals = [22.0, 21.3, 23.9, 22.4, 21.3, 23.9]
        self.put([{"ts": A + 3600 * i, "vonku": 1.0, "obyvacka": v} for i, v in enumerate(vals)])
        p = self.rep()["pasma"][0]
        o = p["obyvacka"]
        self.assertEqual((o["min"], o["max"], o["n"]), (21.3, 23.9, 6))
        self.assertEqual(o["avg"], round(sum(vals) / 6, 1))
        self.assertEqual(o["min_ts"], A + 3600 * 4)      # pri zhode najnovšia hodina
        self.assertEqual(o["max_ts"], A + 3600 * 5)
        self.assertEqual(p["vonku_avg"], 1.0)

    def test_stlpec_s_malo_udajmi_je_none(self):
        self.put([{"ts": A + 3600 * i, "vonku": 1.0, "obyvacka": 22.0, "detska": 21.0 if i < 2 else None} for i in range(4)])
        r = self.rep()
        self.assertIsNone(r["pasma"][0]["detska"])
        self.assertNotIn("detska", [s["key"] for s in r["stlpce"]])
        self.assertIn("obyvacka", [s["key"] for s in r["stlpce"]])

    def test_stlpce_a_voda(self):
        self.put([{"ts": A + 3600 * i, "vonku": 1.0, "obyvacka": 22.0, "chodba": 20.0, "tw2": 35.0, "kwh": 1.0,
                   "beh_min": 30.0} for i in range(3)])
        r = self.rep()
        self.assertEqual([(s["key"], s["skupina"]) for s in r["stlpce"]],
                         [("obyvacka", "izba"), ("chodba", "izba"), ("tw2", "voda")])
        self.assertEqual(r["stlpce"][0]["name"], "Obývačka")
        self.assertEqual(r["stlpce"][1]["name"], "Chodba")
        self.assertEqual(r["stlpce"][2]["name"], "Voda výstup")
        p = r["pasma"][0]
        self.assertEqual(p["kwh_h"], 1.0)
        self.assertEqual(p["beh_pct"], 50)
        self.assertIsNone(p["tw1"])

    def test_kwh_menej_ako_3(self):
        self.put([{"ts": A + 3600 * i, "vonku": 1.0, "obyvacka": 22.0, "kwh": 1.0 if i < 2 else None} for i in range(4)])
        p = self.rep()["pasma"][0]
        self.assertIsNone(p["kwh_h"])
        self.assertIsNone(p["beh_pct"])

    def test_rozsah_datumov(self):
        rows = [{"ts": A + 86400 * d + 3600 * i, "vonku": 1.0, "obyvacka": 22.0} for d in range(3) for i in range(3)]
        self.put(rows)
        r = self.rep(od="2026-11-04", do="2026-11-04")
        self.assertEqual(r["hodin_spolu"], 3)
        self.assertEqual((r["prvy_den"], r["posledny_den"]), ("2026-11-04", "2026-11-04"))

    def test_predvolene_od_najstarsieho(self):
        self.put([{"ts": A + 3600 * i, "vonku": 1.0, "obyvacka": 22.0} for i in range(3)])
        r = vonku.report(self.cfg, now=NOW)
        self.assertEqual((r["od"], r["do"], r["krok"]), ("2026-11-03", "2026-11-14", 2))
        self.assertEqual(r["hodin_spolu"], 3)


class Chyby(Base):
    def test_zly_krok(self):
        for k in (0, 4, 10, "x", None):
            with self.assertRaises(ValueError):
                self.rep(krok=k)

    def test_krok_ako_text(self):
        self.assertEqual(self.rep(krok="5")["krok"], 5)

    def test_do_pred_od(self):
        with self.assertRaises(ValueError):
            self.rep(od="2026-11-05", do="2026-11-03")

    def test_zly_datum(self):
        with self.assertRaises(ValueError):
            self.rep(od="blbost")

    def test_chybajuca_db(self):
        r = vonku.report({"STATS_DB": os.path.join(self.tmp.name, "neexistuje", "x.db")}, now=NOW)
        self.assertEqual((r["pasma"], r["stlpce"], r["hodin_spolu"]), ([], [], 0))
        self.assertIsNone(r["prvy_den"])

    def test_prazdna_db(self):
        stats.connect(self.cfg).close()
        r = self.rep()
        self.assertEqual(r["pasma"], [])

    def test_rychlost(self):
        rows = [{"ts": A + 3600 * i, "vonku": (i % 30) - 10.0, "obyvacka": 22.0 + (i % 7) * 0.1, "tw2": 35.0} for i in range(10000)]
        self.put(rows)
        t = time.time()
        r = vonku.report(self.cfg, od="2026-11-03", do="2027-12-31", now=NOW)
        self.assertLess(time.time() - t, 1.0)
        self.assertGreater(len(r["pasma"]), 5)


# ---------------------------------------------------------------- SPEC_akokuri: vonku.kurenie (tc.db s minútovými vzorkami)
import sqlite3

T0 = stats._midnight(dt.date(2026, 11, 10))
NAST_OK = {"FSV 2011 Vonku najchladnejšie": -15, "FSV 2012 Vonku najteplejšie": 15, "FSV 2031 WL2 voda pri najchladnejšom": 46,
           "FSV 2032 WL2 voda pri najteplejšom": 35, "FSV 4013 Vypnutie kúrenia nad vonk. teplotou": 17}


class Kurenie(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "tc.db")
        self.cfg = {"TC_DB": self.path}
        con = sqlite3.connect(self.path)
        con.execute("CREATE TABLE samples(ts INTEGER, entity TEXT, value REAL)")
        con.execute("CREATE INDEX ix ON samples(ts)")
        con.execute("CREATE TABLE events(ts INTEGER, kind TEXT, entity TEXT, old TEXT, new TEXT, text TEXT)")
        con.commit()
        con.close()
        self.i = 0          # počet už zapísaných minút
        self.rows = []
        self.evs = [(T0 - 60, "state", stats.VALVE, "", "Room", "")]

    def tearDown(self):
        self.tmp.cleanup()

    def seg(self, n, vonku, freq=50.0, tw2=lambda i: 40.0, nast=None, vonku_every=1):
        """n minút: vonku, frekvencia, tw2(i) (i = poradie minúty od začiatku)."""
        for _ in range(n):
            i = self.i
            ts = T0 + 60 * i
            if i % vonku_every == 0:
                self.rows.append((ts, "Vonkajšia teplota", vonku))
            self.rows += [(ts, "Frekvencia kompresora", freq), (ts, "Výstupná teplota vody (Tw2)", tw2(i)),
                          (ts, "Spiatočka (Tw1)", tw2(i) - 5), (ts, "Cieľová ekvitermická voda (WL)", 42.0),
                          (ts, "Posun ekvitermickej krivky", -1.0 if i % 2 else 2.0)]
            for k, v in (nast if nast is not None else NAST_OK).items():
                self.rows.append((ts, k, v(i) if callable(v) else v))
            self.i += 1

    def ev(self, minute, entity, new):
        self.evs.append((T0 + 60 * minute, "state", entity, "", new, ""))

    def run_k(self, **kw):
        con = sqlite3.connect(self.path)
        con.executemany("INSERT INTO samples VALUES(?,?,?)", self.rows)
        con.executemany("INSERT INTO events VALUES(?,?,?,?,?,?)", self.evs)
        con.commit()
        con.close()
        kw.setdefault("od", "2026-11-10")
        kw.setdefault("do", "2026-11-10")
        return vonku.kurenie(self.cfg, now=NOW, **kw)

    def test_zakladne_pasmo_percentily_a_beh(self):
        self.seg(100, -2.5, tw2=lambda i: 30 + i * 0.1)      # kúri od 11. minúty (index 10): 90 minút
        self.seg(100, -2.5, freq=0.0)                        # stojí v režime kúrenia -> beh 50 %
        r = self.run_k()
        self.assertEqual(len(r["pasma"]), 1)
        p = r["pasma"][0]
        self.assertEqual((p["od_c"], p["do_c"], p["minut_kurenia"], p["vonku_avg"]), (-3, -2, 90, -2.5))
        self.assertEqual((p["tw2"]["abs_min"], p["tw2"]["abs_max"]), (31.0, 39.9))
        self.assertAlmostEqual(p["tw2"]["min"], 31.4, delta=0.06)
        self.assertAlmostEqual(p["tw2"]["max"], 39.5, delta=0.06)
        self.assertAlmostEqual(p["tw2"]["avg"], 35.5, delta=0.06)
        self.assertGreater(p["tw2"]["min"], p["tw2"]["abs_min"])
        self.assertLess(p["tw2"]["max"], p["tw2"]["abs_max"])
        self.assertEqual(p["tw2"]["min_ts"], T0 + 60 * 14)    # minúta najbližšia 5. percentilu (31,4)
        self.assertEqual((p["wl"]["min"], p["wl"]["max"]), (42.0, 42.0))
        self.assertAlmostEqual(p["tw1"]["avg"], 30.45, delta=0.06)
        self.assertEqual(p["posun"], {"min": -1.0, "max": 2.0})   # v minútach režimu kúrenia (aj mimo behu)
        self.assertEqual(p["beh_pct"], 50)
        self.assertEqual(r["minut_spolu"], 90)
        self.assertEqual(r["prva_vzorka"], "2026-11-10")
        self.assertEqual(r["nastavenie"], {"fsv2011": -15, "fsv2012": 15, "fsv2031": 46, "fsv2032": 35, "fsv4013": 17})
        self.assertEqual(r["krivka_nastavenie"]["fsv2031"], 46)
        self.assertIsNone(r["krivka_nastavenie"]["fsv2021"])
        self.assertEqual(r["zmeny"], [])

    def test_tuv_sa_nerata(self):
        self.seg(150, -2.5)
        self.ev(50, stats.VALVE, "Tank")
        self.ev(70, stats.VALVE, "Room")
        p = self.run_k()["pasma"][0]
        self.assertEqual(p["minut_kurenia"], 40 + 80)         # 10..49 a 70..149
        self.assertEqual(p["beh_pct"], 100)

    def test_odmrazovanie_a_10_min_po_nom(self):
        self.seg(150, -2.5)
        self.ev(60, stats.DEFROST, "ON")
        self.ev(65, stats.DEFROST, "OFF")
        r = self.run_k()
        self.assertEqual(r["pasma"][0]["minut_kurenia"], 50 + 76)   # 10..59 a 74..149 (60..73 vynechané)

    def test_rozbeh_sa_nerata(self):
        self.seg(40, -2.5, freq=0.0)
        self.seg(30, -2.5)                                    # 30 minút behu -> 19 po rozbehu (< 30, pásmo sa vynechá)
        self.seg(20, -2.5, freq=0.0)
        self.assertEqual(self.run_k()["pasma"], [])
        self.tearDown()
        self.setUp()
        self.seg(41, -2.5)                                    # 41 - 10 = 31 minút
        self.assertEqual(self.run_k()["pasma"][0]["minut_kurenia"], 31)

    def test_pasma_zoradenie_krok_a_male_pasmo(self):
        self.seg(60, 5.5)
        self.seg(5, 5.5, freq=0.0)
        self.seg(60, -7.2)
        self.seg(5, -7.2, freq=0.0)
        self.seg(35, 1.0)                                     # 25 minút kúrenia -> vynechané
        r = self.run_k(krok=5)
        self.assertEqual([(p["od_c"], p["do_c"], p["minut_kurenia"]) for p in r["pasma"]], [(-10, -5, 50), (5, 10, 50)])
        r1 = vonku.kurenie(self.cfg, od="2026-11-10", do="2026-11-10", krok=1, now=NOW)
        self.assertEqual([p["od_c"] for p in r1["pasma"]], [-8, 5])

    def test_vonku_sa_dopina_poslednou_hodnotou(self):
        self.seg(100, -2.5, vonku_every=10)
        self.assertEqual(self.run_k()["pasma"][0]["minut_kurenia"], 90)

    def test_vonku_prilis_stara(self):
        self.seg(100, -2.5, vonku_every=1000)                 # vonku je len v minúte 0; po 15 minútach sa prestane dopĺňať
        self.assertEqual(self.run_k()["pasma"], [])      # 6 minút kúrenia (10..15) < 30

    def test_krivka_interpolacia_a_orezanie(self):
        s = {"fsv2011": -15, "fsv2012": 15, "fsv2031": 46, "fsv2032": 35}
        self.assertEqual(vonku._krivka(-15, s), 46.0)
        self.assertEqual(vonku._krivka(-30, s), 46.0)
        self.assertEqual(vonku._krivka(0, s), 40.5)
        self.assertEqual(vonku._krivka(-2.5, s), 41.4)
        self.assertEqual(vonku._krivka(15, s), 35.0)
        self.assertEqual(vonku._krivka(22, s), 35.0)
        self.assertIsNone(vonku._krivka(0, {"fsv2011": -15}))
        self.seg(60, -2.5)
        self.seg(5, -2.5, freq=0.0)
        self.seg(60, 18.5)
        r = self.run_k()
        self.assertEqual([(p["krivka"], p["nad_vypnutim"]) for p in r["pasma"]], [(41.4, False), (35.0, True)])

    def test_zmeny_nastavenia(self):
        nast = dict(NAST_OK)
        nast["FSV 2031 WL2 voda pri najchladnejšom"] = lambda i: 46 if i < 30 else 44
        nast["Maximálny kladný posun krivky"] = lambda i: 2 if i < 50 else 3
        nast["Zosilnenie posunu krivky"] = 3
        self.seg(60, -2.5, nast=nast)
        r = self.run_k()
        self.assertEqual([(z["nazov"], z["old"], z["new"], z["ts"]) for z in r["zmeny"]],
                         [("Maximálny kladný posun krivky", 2.0, 3.0, T0 + 50 * 60),
                          ("FSV 2031 WL2 voda pri najchladnejšom", 46.0, 44.0, T0 + 30 * 60)])   # najnovšie prvé; posun sa nezapisuje
        k = r["krivka_nastavenie"]
        self.assertEqual((k["fsv2031"], k["max_posun_kladny"], k["zosilnenie"], k["posun"]), (44, 3, 3, -1.0))
        self.assertEqual(r["pasma"][0]["krivka"], round(44 + (35 - 44) * 12.5 / 30, 1))

    def test_zmeny_max_50(self):
        nast = {"FSV 2031 WL2 voda pri najchladnejšom": lambda i: 40 + (i % 2)}
        self.seg(120, -2.5, nast=nast)
        self.assertEqual(len(self.run_k()["zmeny"]), 50)

    def test_chybajuca_alebo_prazdna_db(self):
        r = vonku.kurenie({"TC_DB": os.path.join(self.tmp.name, "nie", "x.db")}, now=NOW)
        self.assertEqual((r["pasma"], r["zmeny"], r["minut_spolu"], r["prva_vzorka"]), ([], [], 0, None))
        r = vonku.kurenie(self.cfg, now=NOW)            # prázdne tabuľky
        self.assertEqual((r["pasma"], r["prva_vzorka"]), ([], None))

    def test_predvolene_od_prvej_vzorky(self):
        self.seg(100, -2.5)
        self.run_k()
        r = vonku.kurenie(self.cfg, now=NOW)
        self.assertEqual((r["od"], r["do"], r["krok"]), ("2026-11-10", "2026-11-14", 1))
        self.assertEqual(r["pasma"][0]["minut_kurenia"], 90)

    def test_chyby(self):
        for k in (0, 3, 10, "x", None):
            with self.assertRaises(ValueError):
                vonku.kurenie(self.cfg, krok=k, now=NOW)
        with self.assertRaises(ValueError):
            vonku.kurenie(self.cfg, od="2026-11-05", do="2026-11-03", now=NOW)
        with self.assertRaises(ValueError):
            vonku.kurenie(self.cfg, od="blbost", now=NOW)
        with self.assertRaises(ValueError) as c:
            vonku.kurenie(self.cfg, od="2025-01-01", do="2026-11-10", now=NOW)
        self.assertIn("400", str(c.exception))
        self.assertEqual(vonku.kurenie(self.cfg, krok="2", now=NOW)["krok"], 2)

    def test_rychlost_30_dni(self):
        self.seg(43200, 0.5, tw2=lambda i: 38 + (i % 50) * 0.1, nast={})      # 6 entít x 43 200 minút (SPEC); nastavenia krivky sa nepridávajú
        con = sqlite3.connect(self.path)
        con.executemany("INSERT INTO samples VALUES(?,?,?)", self.rows)
        con.executemany("INSERT INTO events VALUES(?,?,?,?,?,?)", self.evs)
        con.commit()
        con.close()
        t = time.time()
        r = vonku.kurenie(self.cfg, od="2026-11-10", do="2026-12-09", now=NOW)
        self.assertLess(time.time() - t, 2.0)
        self.assertEqual(r["pasma"][0]["minut_kurenia"], 43200 - 10)

if __name__ == "__main__":
    unittest.main()




