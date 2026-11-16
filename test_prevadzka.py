"""Štítok režimu vedľa loga: app.prevadzka() z poslednej vzorky (frekvencia kompresora) a udalostí (ventil, odmrazovanie)."""
import os, sqlite3, tempfile, unittest

os.environ.setdefault("DRY_RUN", "1")
import app
import profil
from common import S

NOW = 1_800_000_000
FREQ = S.get("freq", "Frekvencia kompresora")
VENTIL = profil.device_entity("ventil")
ODMRAZ = profil.device_entity("odmrazovanie")


class _Db(unittest.TestCase):
    """Dočasná tc.db so samples, events a meta."""
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.db = os.path.join(self.d, "tc.db")
        c = sqlite3.connect(self.db)
        c.executescript("CREATE TABLE samples(ts INTEGER, entity TEXT, value REAL);"
                        "CREATE TABLE events(ts INTEGER, kind TEXT, entity TEXT, old TEXT, new TEXT, text TEXT);"
                        "CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);")
        c.commit(); c.close()
        self.cfg = {"TC_DB": self.db}

    def put(self, hz_by_min, ventil=None, odmraz=None, last=NOW - 30):
        c = sqlite3.connect(self.db)
        ts = last - 60 * (len(hz_by_min) - 1)
        for hz in hz_by_min:
            c.execute("INSERT INTO samples VALUES(?,?,?)", (ts, FREQ, hz)); ts += 60
        c.execute("INSERT OR REPLACE INTO meta VALUES('last_sample', ?)", (str(last),))
        for ent, ev in ((VENTIL, ventil), (ODMRAZ, odmraz)):
            if ev:
                c.execute("INSERT INTO events VALUES(?,?,?,?,?,NULL)", (ev[0], "ovladanie", ent, "", ev[1]))
        c.commit(); c.close()


class Prevadzka(_Db):
    def test_kurenie(self):
        self.put([0, 0, 40, 41, 42], ventil=(NOW - 3600, "Room (Heating)"))
        p = app.prevadzka(self.cfg, NOW)
        self.assertEqual(p["rezim"], "kurenie")
        self.assertEqual(p["od"], NOW - 30 - 3 * 60 + 60)      # minúta po poslednej vzorke s 0 Hz

    def test_tuv(self):
        self.put([45, 46, 47], ventil=(NOW - 120, "Tank (DHW)"))
        p = app.prevadzka(self.cfg, NOW)
        self.assertEqual((p["rezim"], p["od"]), ("tuv", NOW - 120))

    def test_odmrazovanie_ma_prednost(self):
        self.put([30, 31], ventil=(NOW - 600, "Room (Heating)"), odmraz=(NOW - 90, "ON"))
        self.assertEqual(app.prevadzka(self.cfg, NOW)["rezim"], "odmrazovanie")

    def test_stoji(self):
        self.put([40, 0, 0], ventil=(NOW - 600, "Tank (DHW)"))
        p = app.prevadzka(self.cfg, NOW)
        self.assertEqual((p["rezim"], p["od"]), ("stoji", NOW - 30 - 2 * 60 + 60))

    def test_stare_data_bez_stitku(self):
        self.put([40], last=NOW - 3600)
        self.assertIsNone(app.prevadzka(self.cfg, NOW)["rezim"])

    def test_bez_databazy(self):
        self.assertEqual(app.prevadzka({"TC_DB": os.path.join(self.d, "nie.db")}, NOW), {"rezim": None, "od": None})


class OhrevyTuv(_Db):
    """common.dhw_intervals a minúty TÚV v hour_stats (vstup agenta)."""

    def ev(self, ts, new):
        c = sqlite3.connect(self.db)
        c.execute("INSERT INTO events VALUES(?,?,?,?,?,NULL)", (ts, "ovladanie", VENTIL, "", new)); c.commit(); c.close()

    def test_intervaly(self):
        import common
        a = NOW - 4 * 3600
        self.put([40] * 5)
        self.ev(a - 600, "Tank (DHW)")          # ohrev začal pred oknom -> začiatok okna
        self.ev(a + 600, "Room (Heating)")
        self.ev(a + 3000, "Tank (DHW)")
        self.ev(a + 3060, "Tank (DHW)")          # opakované hlásenie Tank nič nemení
        self.ev(a + 4800, "Room (Heating)")
        self.ev(a + 7200, "Tank (DHW)")
        self.ev(a + 7260, "Room (Heating)")      # 60 s = zákmit, nepočíta sa
        self.ev(NOW - 300, "Tank (DHW)")         # prebieha: koniec = posledná vzorka + 60 s
        self.assertEqual(common.dhw_intervals(self.cfg, a, NOW + 60),
                         [(a, a + 600), (a + 3000, a + 4800), (NOW - 300, NOW + 30)])

    def test_minuty_v_hodinach(self):
        import common
        a = NOW - NOW % 3600 - 2 * 3600
        c = sqlite3.connect(self.db)
        for ts in range(a, a + 3 * 3600, 60):
            c.execute("INSERT INTO samples VALUES(?,?,?)", (ts, FREQ, 40))
        c.execute("INSERT OR REPLACE INTO meta VALUES('last_sample', ?)", (str(a + 3 * 3600 - 60),)); c.commit(); c.close()
        self.ev(a + 3000, "Tank (DHW)")
        self.ev(a + 3600 + 1200, "Room (Heating)")   # 10 min v 1. hodine, 20 min v 2.
        h = common.hour_stats(self.cfg, a, a + 3 * 3600)
        self.assertEqual([x.get("tuv_min") for x in h], [10, 20, 0])


if __name__ == "__main__":
    unittest.main()
