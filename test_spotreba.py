#!/usr/bin/env python3
"""Testy SPEC_spotreba: spotreba.report (prednosť stats.db pred ims, zmena času, cena, neúplný deň, ostatné, mesiace, limity).
Dočasná stats.db a ims súbor; nič nezapisuje do ostrých databáz.
Spustenie: python test_spotreba.py   (v priečinku kurenia)"""
import datetime as dt, json, os, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ha_cost, spotreba, stats

NOW = int(dt.datetime(2026, 11, 14, 12, 0).timestamp())


def mid(y, m, d):
    return stats._midnight(dt.date(y, m, d))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {"STATS_DB": os.path.join(self.tmp.name, "stats.db"), "BILLING_START": "11-01"}
        self.ims_path = os.path.join(self.tmp.name, "ims.json")
        self._old_ims = ha_cost.IMS_FILE
        ha_cost.IMS_FILE = self.ims_path
        self.ims = {}
        self.rows = []

    def tearDown(self):
        ha_cost.IMS_FILE = self._old_ims
        self.tmp.cleanup()

    def save(self):
        with open(self.ims_path, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in self.ims.items()}, f)
        if self.rows:
            stats.upsert_hourly(self.cfg, self.rows)

    def rep(self, od, do):
        self.save()
        return spotreba.report(self.cfg, od, do, NOW)

    def day(self, r, i):
        return r["dni"][i]


class Zdroje(Base):
    def test_stats_ma_prednost_pred_ims(self):
        a = mid(2026, 11, 3)
        self.rows = [{"ts": a, "kwh_dom": 1.0}, {"ts": a + 3600, "kwh_dom": 2.0}]
        self.ims = {a: 5.0, a + 3600: 7.0, a + 7200: 0.5}      # tretia hodina je len v ims
        d = self.day(self.rep("2026-11-03", "2026-11-03"), 0)
        self.assertAlmostEqual(d["dom_kwh"], 3.5, places=6)    # 1,0 + 2,0 + 0,5
        self.assertEqual(d["dom_hodin"], 3)

    def test_len_ims(self):
        a = mid(2026, 11, 3)
        self.ims = {a + 3600 * i: 0.25 for i in range(24)}
        d = self.day(self.rep("2026-11-03", "2026-11-03"), 0)
        self.assertEqual(d["dom_kwh"], 6.0)
        self.assertTrue(d["kompletny"])
        self.assertIsNone(d["cerpadlo_kwh"])
        self.assertIsNone(d["ostatne_kwh"])

    def test_bez_udajov(self):
        r = self.rep("2026-11-03", "2026-11-04")
        self.assertEqual(r["spolu"]["dni_s_udajmi"], 0)
        self.assertIsNone(r["spolu"]["dom_kwh"])
        self.assertIsNone(r["spolu"]["priemer_den_kwh"])
        self.assertIsNone(r["posledny_den_s_udajmi"])
        self.assertIsNone(self.day(r, 0)["cena_eur"])
        self.assertEqual(self.day(r, 0)["den_tyzdna"], "ut")


class Cas(Base):
    def test_den_s_25_hodinami(self):
        a, b = stats._bounds(dt.date(2026, 10, 25))
        if (b - a) != 25 * 3600:
            self.skipTest("lokálna časová zóna nemá zmenu času 25. 10. 2026")
        self.ims = {a + 3600 * i: 1.0 for i in range(25)}
        d = self.day(self.rep("2026-10-25", "2026-10-25"), 0)
        self.assertEqual(d["hodin"], 25)
        self.assertEqual(d["dom_hodin"], 25)
        self.assertEqual(d["dom_kwh"], 25.0)
        self.assertTrue(d["kompletny"])
        # susedné dni nesmú zachytiť hodiny dňa so zmenou času
        r = self.rep("2026-10-24", "2026-10-26")
        self.assertEqual(r["dni"][0]["dom_hodin"], 0)
        self.assertEqual(r["dni"][2]["dom_hodin"], 0)

    def test_den_s_23_hodinami(self):
        a, b = stats._bounds(dt.date(2026, 3, 29))
        if (b - a) != 23 * 3600:
            self.skipTest("lokálna časová zóna nemá zmenu času 29. 3. 2026")
        self.ims = {a + 3600 * i: 1.0 for i in range(23)}
        d = self.day(self.rep("2026-03-29", "2026-03-29"), 0)
        self.assertEqual((d["hodin"], d["dom_hodin"], d["dom_kwh"]), (23, 23, 23.0))
        self.assertTrue(d["kompletny"])


class Cena(Base):
    def test_cena_je_sucet_kwh_krat_cena_plus_poplatok(self):
        a = mid(2026, 11, 3)
        kw = [0.5 + 0.1 * i for i in range(24)]
        self.ims = {a + 3600 * i: kw[i] for i in range(24)}
        d = self.day(self.rep("2026-11-03", "2026-11-03"), 0)
        price, fee = ha_cost.tariff_at(self.cfg, a)
        self.assertAlmostEqual(d["cena_eur"], round(sum(kw) * price + 24 * fee, 2), places=2)

    def test_neuplny_den_ma_poplatok_za_vsetky_hodiny(self):
        a = mid(2026, 11, 3)
        self.ims = {a + 3600 * i: 1.0 for i in range(10)}
        d = self.day(self.rep("2026-11-03", "2026-11-03"), 0)
        price, fee = ha_cost.tariff_at(self.cfg, a)
        self.assertFalse(d["kompletny"])
        self.assertEqual(d["dom_hodin"], 10)
        self.assertAlmostEqual(d["cena_eur"], round(10 * price + 24 * fee, 2), places=2)

    def test_cena_cerpadla_a_energopomoc(self):
        a = mid(2026, 11, 3)
        self.cfg["ENERGY_ENERGOPOMOC"] = "1"
        self.cfg["ENERGY_VT_SHARE"] = "0.5"
        self.rows = [{"ts": a + 3600 * i, "kwh_dom": 1.0, "kwh": 0.3, "kwh_cal": 0.4} for i in range(24)]
        r = self.rep("2026-11-03", "2026-11-03")
        price, fee = ha_cost.tariff_at(self.cfg, a)
        self.assertAlmostEqual(self.day(r, 0)["cena_cerpadlo_eur"], round(24 * 0.4 * price, 2), places=2)
        c = r["cennik"]
        self.assertTrue(c["energopomoc"])
        self.assertEqual(c["vt_podiel"], 0.5)
        self.assertEqual(c["platny_od"], "2026-01-01")
        self.assertEqual((c["vt"], c["nt"]), (0.2502109, 0.1376488))
        self.assertAlmostEqual(c["vazena_cena"], price, places=4)
        self.assertAlmostEqual(c["poplatok_den"], fee * 24, places=4)
        self.assertEqual(c["mesacna_platba"], 12.9234)

    def test_fee_month_prepise_mesacnu_platbu(self):
        self.cfg["ENERGY_FEE_MONTH"] = "10"
        r = self.rep("2026-11-03", "2026-11-03")
        self.assertEqual(r["cennik"]["mesacna_platba"], 10.0)


class Cerpadlo(Base):
    def test_ostatne_je_dom_minus_cerpadlo(self):
        a = mid(2026, 11, 3)
        self.rows = [{"ts": a + 3600 * i, "kwh_dom": 1.5, "kwh": 0.9, "kwh_cal": 0.5, "q_kwh": 1.5, "vonku": 2.0 + i % 2}
                     for i in range(24)]
        d = self.day(self.rep("2026-11-03", "2026-11-03"), 0)
        self.assertEqual(d["dom_kwh"], 36.0)
        self.assertEqual(d["cerpadlo_kwh"], 12.0)             # kwh_cal má prednosť pred kwh
        self.assertEqual(d["ostatne_kwh"], 24.0)
        self.assertEqual(d["teplo_kwh"], 36.0)
        self.assertEqual(d["cop"], 3.0)
        self.assertEqual(d["vonku"], 2.5)

    def test_kwh_ked_nie_je_kalibrovane(self):
        a = mid(2026, 11, 3)
        self.rows = [{"ts": a + 3600 * i, "kwh_dom": 1.0, "kwh": 0.25} for i in range(24)]
        d = self.day(self.rep("2026-11-03", "2026-11-03"), 0)
        self.assertEqual(d["cerpadlo_kwh"], 6.0)
        self.assertIsNone(d["teplo_kwh"])
        self.assertIsNone(d["cop"])

    def test_malo_hodin_cerpadla_nema_ostatne(self):
        a = mid(2026, 11, 3)
        self.rows = [{"ts": a + 3600 * i, "kwh_dom": 1.0, "kwh": 0.5} for i in range(19)] + \
                    [{"ts": a + 3600 * i, "kwh_dom": 1.0} for i in range(19, 24)]
        d = self.day(self.rep("2026-11-03", "2026-11-03"), 0)
        self.assertEqual(d["cerpadlo_kwh"], 9.5)
        self.assertIsNone(d["ostatne_kwh"])
        self.rows = [{"ts": a + 3600 * i, "kwh_dom": 1.0, "kwh": 0.5} for i in range(20)]
        stats.upsert_hourly(self.cfg, self.rows)
        d = self.day(self.rep("2026-11-03", "2026-11-03"), 0)
        self.assertEqual(d["ostatne_kwh"], 14.0)              # 24 − 10


class Sucty(Base):
    def test_mesacne_sucty_a_spolu(self):
        a = mid(2026, 10, 31)
        self.rows = []
        for dd in range(4):                                    # 31. 10., 1. 11., 2. 11., 3. 11.
            for i in range(24):
                self.rows.append({"ts": stats._bounds(dt.date(2026, 10, 31) + dt.timedelta(days=dd))[0] + 3600 * i,
                                  "kwh_dom": 1.0, "kwh": 0.25})
        r = self.rep("2026-10-31", "2026-11-04")
        m = {x["mesiac"]: x for x in r["mesiace"]}
        self.assertEqual(sorted(m), ["2026-10", "2026-11"])
        self.assertEqual(m["2026-10"]["dom_kwh"], 24.0)
        self.assertEqual(m["2026-11"]["dom_kwh"], 72.0)
        self.assertEqual(m["2026-11"]["cerpadlo_kwh"], 18.0)
        self.assertEqual(m["2026-11"]["dni_s_udajmi"], 3)
        sp = r["spolu"]
        self.assertEqual(sp["dom_kwh"], 96.0)
        self.assertEqual(sp["dni_s_udajmi"], 4)
        self.assertEqual(sp["priemer_den_kwh"], 24.0)
        self.assertAlmostEqual(sp["cena_eur"], round(sum(x["cena_eur"] for x in r["dni"] if x["cena_eur"]), 2), delta=0.02)
        self.assertEqual(sp["ostatne_kwh"], 72.0)
        self.assertEqual(r["posledny_den_s_udajmi"], "2026-11-03")
        self.assertEqual(r["obdobie_zaciatok"], "2026-11-01")

    def test_predvolene_obdobie(self):
        r = self.rep(None, None)
        self.assertEqual((r["od"], r["do"]), ("2026-11-01", "2026-11-14"))
        self.assertEqual(len(r["dni"]), 14)


class Vstup(Base):
    def test_viac_ako_400_dni(self):
        self.save()
        with self.assertRaises(ValueError):
            spotreba.report(self.cfg, "2025-01-01", "2026-11-14", NOW)
        self.assertEqual(len(spotreba.report(self.cfg, "2025-10-11", "2026-11-14", NOW)["dni"]), 400)   # presne 400 ešte ide

    def test_zly_vstup(self):
        self.save()
        for od, do in (("2026-11-05", "2026-11-01"), ("abc", "2026-11-01"), ("2026-13-01", "2026-11-01")):
            with self.assertRaises(ValueError):
                spotreba.report(self.cfg, od, do, NOW)

    def test_bez_stats_db_a_ims(self):
        r = spotreba.report({"STATS_DB": os.path.join(self.tmp.name, "nie.db")}, "2026-11-01", "2026-11-02", NOW)
        self.assertEqual(r["spolu"]["dni_s_udajmi"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
