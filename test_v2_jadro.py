#!/usr/bin/env python3
"""Testy v2 (úloha A1): jadro agenta a zberač podľa profilu (profil.py).
 - predvolený profil = konštanty modulov identické s verziou 1 (test_profil_v1.json),
 - iný profil (3 izby, referenčná obývačka, podlahovka, bez TÚV, bez niektorých entít) sa overí v samostatnom procese
   (moduly si mapy načítajú pri importe), s dočasným profil.json a dočasnou databázou.
Spustenie: python test_v2_jadro.py   (v priečinku kurenia; nepoužíva produkčné dáta)"""
import json, os, sqlite3, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import profil

with open(os.path.join(HERE, "test_profil_v1.json"), encoding="utf-8") as _f:
    V1 = json.load(_f)


def iny_profil():
    """Iný dom: podlahovka, 3 izby (referenčná obývačka), bez TÚV, bez chybového kódu a ventilu."""
    p = profil.default()
    p["zariadenie"].update({"model": "", "nazov": "Samsung EHS Mono 8 kW", "typ": "mono", "vykon_kw": 8.0, "fazy": 1,
                            "chladivo": "R32", "kurenie": "podlahove", "tuv": False})
    p["esp"]["entity"].update({"sledovana": "Sledovaná teplota (Obývačka)", "tuv": "", "tuv_ciel": "", "chyba": "", "ventil": "",
                               "odmrazovanie": "", "flow": "", "kwh_den": "", "freq": ""})
    p["esp"]["rest_extra"] = ["Radiátor Test"]
    p["izby"] = [
        {"kluc": "obyvacka", "nazov": "Obývačka", "esp": "Izba Obývačka (Zigbee)", "referencna": True, "min": None, "ha": ["sensor.ob_t"]},
        {"kluc": "spalna", "nazov": "Spálňa", "esp": "Izba Spálňa", "min": 20.0, "ha": ["sensor.sp_t"]},
        {"kluc": "loznica", "nazov": "Ložnica hostí", "esp": "Izba Ložnica", "min": 19.5, "ha": []},
    ]
    p["ine_teploty"] = {}
    p["elektrina"] = {"ims_stat": "ssd_ims:iny_dom", "meter_ids": ["sensor.em_celkom"]}
    return p


@unittest.skipIf(os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "profil.json")),
                 "inštalácia má vlastný profil.json – test predvoleného profilu sa preskakuje")
class DefaultProfile(unittest.TestCase):
    """Bez profil.json sa moduly správajú ako v1."""

    def test_common_konstanty(self):
        import common
        self.assertFalse(os.path.exists(profil.PROFILE_FILE), "test predpokladá, že profil.json neexistuje")
        s = dict(V1["S"])
        self.assertEqual(common.S, s)
        self.assertEqual(common.INV, {v: k for k, v in s.items()})
        self.assertEqual(common.SETTINGS, V1["SETTINGS"])
        self.assertEqual(list(common.ROOMS), V1["ROOMS"])
        self.assertEqual(common.ROOM_NAMES, V1["ROOM_NAMES"])
        self.assertEqual(list(common.TARGET_ROOMS), V1["TARGET_ROOMS"])
        self.assertEqual(common.TARGET_DEFAULTS, V1["TARGET_DEFAULTS"])
        self.assertEqual((common.REF_ROOM, common.REF_NAME), ("detska", "Detská"))

    def test_agent_konstanty(self):
        import agent
        self.assertEqual({k: list(v) for k, v in agent.ALLOWED.items()}, V1["ALLOWED"])
        self.assertEqual(agent.CURVE_ENTITIES, (V1["SETTINGS"]["fsv2031"], V1["SETTINGS"]["fsv2032"]))
        self.assertEqual(agent.RAISE_CURVE, agent.CURVE_ENTITIES + (V1["SETTINGS"]["max_posun"],))
        self.assertEqual(agent.ROOM_TARGET_ENT, V1["ROOM_TARGET_ENT"])
        self.assertEqual([agent.ROOM_TARGET_MIN, agent.ROOM_TARGET_MAX], V1["ROOM_TARGET"])
        # čítanie z ESP obsahuje všetko, čo v1 (a nič duplicitne)
        v1_names = {"Vonkajšia teplota", "Sledovaná teplota (Detská izba)", "Aktuálna cieľová teplota izby", "Posun ekvitermickej krivky",
                    "Cieľová ekvitermická voda (WL)", "Výstupná teplota vody (Tw2)", "Spiatočka (Tw1)", "Frekvencia kompresora",
                    "Chybový kód TČ", "Teplota TÚV (Bojler)", "Cieľová izbová teplota", "Maximálny posun krivky",
                    "Zosilnenie posunu krivky", "Cieľová teplota TÚV", "FSV 2011 Vonku najchladnejšie", "FSV 2012 Vonku najteplejšie",
                    "Kúrenie", "Ohrev TÚV", "Režim TÚV", "Režim izbovej regulácie", "Stav izbovej regulácie", "Režim vonkajšej jednotky",
                    "3-cestný ventil (Smer ohrevu)", "Obehové čerpadlo", "Elektrický dohrev kúrenia (Backup heater)",
                    "Elektrický dohrev TÚV (Booster heater)", "Odmrazovanie"} | set(V1["ALLOWED"])
        got = [n for _d, n in agent.READ_NOW]
        self.assertEqual(set(got), v1_names)
        self.assertEqual(len(got), len(set(got)))

    def test_prompty(self):
        import agent
        for txt in (agent.SYSTEM, agent.LEARN_SYSTEM, agent.CHAT_SYSTEM, agent.CHAT_SYSTEM_USER, agent.WEEKLY_SYSTEM):
            self.assertNotIn("@@", txt)
        self.assertIn("detska|jedalen|kuchyna|obyvacka|spalna|pracovna|kupelna", agent.CHAT_SYSTEM)
        self.assertIn("Samsung EHS Split 12 kW", agent.SYSTEM)
        self.assertIn("radiátory", agent.SYSTEM)
        txt = agent.SYSTEM.format(allowed="A", targets="T")       # šablóna musí prejsť cez str.format
        self.assertIn("23-23,5 °C", txt)
        self.assertIn("priškrtil ventily", txt)

    def test_zberac_a_ostatne(self):
        import collector, contacts, dhw, ha_ws
        self.assertEqual(set(collector.REST_POLL), {"Izba Kuchyňa", "Izba Jedáleň", "Radiátor Jedáleň (hlavica)", "Izba Obývačka",
                                                    "Izba Detská (Zigbee)", "Izba Kúpeľňa", "Izba Spálňa", "Izba Pracovňa"})
        self.assertEqual(dhw.ENT_TARGET, "Cieľová teplota TÚV")
        self.assertEqual(dhw.ENT_ERROR, "Chybový kód TČ")
        self.assertEqual([r for _n, r in contacts.DEFAULT_GROUPS],
                         ["obyvacka", "detska", "jedalen", "kuchyna", "spalna", "pracovna", "kupelna", None, None])
        self.assertEqual(contacts.KEYWORDS, (("obyvack", "obyvacka"), ("detsk", "detska"), ("jedal", "jedalen"), ("kuchyn", "kuchyna"),
                                             ("spaln", "spalna"), ("pracovn", "pracovna"), ("kupel", "kupelna"), ("chodb", "chodba")))
        if "ENERGY_EXT_ID" not in os.environ:
            self.assertEqual(ha_ws.ENERGY_EXT_ID, "ssd_ims:odberne_miesto_actual_consumption")


# ---------------------------------------------------------------- iný profil v samostatnom procese
CHILD = r'''
import json, os, sys, sqlite3, time
sys.path.insert(0, %(here)r)
import profil
profil.PROFILE_FILE = %(pfile)r
import common, agent, collector, abtest, interventions, contacts, dhw, ha_ws, notify
out = {}
out["S"] = common.S
out["ROOMS"] = list(common.ROOMS)
out["ROOM_NAMES"] = common.ROOM_NAMES
out["TARGET_ROOMS"] = list(common.TARGET_ROOMS)
out["TARGET_DEFAULTS"] = common.TARGET_DEFAULTS
out["ALLOWED"] = {k: list(v) for k, v in agent.ALLOWED.items()}
out["READ_NOW"] = [n for _d, n in agent.READ_NOW]
out["ROOM_TARGET"] = [agent.ROOM_TARGET_MIN, agent.ROOM_TARGET_MAX]
out["SYSTEM"] = agent.SYSTEM.format(allowed="A", targets="T")
out["CHAT"] = agent.CHAT_SYSTEM
out["CHAT_USER"] = agent.CHAT_SYSTEM_USER
out["REST_POLL"] = collector.REST_POLL
out["DHW"] = [dhw.ENT_TARGET, dhw.TUV_ON]
out["GROUPS"] = [r for _n, r in contacts.DEFAULT_GROUPS]
out["KEYWORDS"] = [list(x) for x in contacts.KEYWORDS]
out["ENERGY"] = [ha_ws.ENERGY_EXT_ID, list(ha_ws.ENERGY_METER_IDS)]
out["CURVE"] = list(agent.CURVE_ENTITIES)
# zápis: nepovolené entity sa odmietnu (aj prázdny názov), TÚV nie je povolené
errs = []
for n in ("", "Cieľová teplota TÚV", "Teplota TÚV (Bojler)"):
    try:
        agent.esp_set_number({"DRY_RUN": "1", "ESP_URL": "http://x"}, n, "1")
        errs.append("zapisane:" + n)
    except ValueError:
        pass
out["WRITE_ERRS"] = errs
out["DHW_TICK"] = dhw.tick({"DRY_RUN": "1"}, None, 0)
out["CAN_START"] = list(dhw.can_start({}, None))
# hour_stats / day_summary s chýbajúcimi entitami (freq, spotreba, dt)
db = %(db)r
cfg = {"TC_DB": db, "AGENT_DB": %(adb)r}
con = sqlite3.connect(db)
con.execute("CREATE TABLE samples(ts INTEGER, entity TEXT, value REAL)")
con.execute("CREATE TABLE events(ts INTEGER, kind TEXT, entity TEXT, old TEXT, new TEXT, text TEXT)")
a, b = common.day_bounds("2026-02-10")
for h in range(24):
    ts = a + h * 3600 + 60
    ref = 20.0 if h < 10 else 22.0
    for ent, v in ((common.S["vonku"], -2.0), (common.S["obyvacka"], ref), (common.S["spalna"], ref - 1.5), (common.S["loznica"], 19.0),
                   (common.S["ciel"], 22.0)):
        con.execute("INSERT INTO samples VALUES(?,?,?)", (ts, ent, v))
con.execute("INSERT INTO samples VALUES(?,?,?)", (a + 100, common.SETTINGS["ciel_izba"], 22.0))
con.commit(); con.close()
s = common.day_summary(cfg, a, b)
out["SUM"] = {k: s.get(k) for k in ("obyvacka", "spalna", "loznica", "obyvacka_min", "obyvacka_max", "hod_pod_cielom", "hod_nad_cielom",
                                     "rozdiel_spalna", "rozdiel_loznica", "ciel_izba", "beh", "kwh")}
out["HAS_DETSKA"] = "detska" in s or "rozdiel_detska" in s
print(json.dumps(out, ensure_ascii=False))
'''


class InyProfil(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        pfile = os.path.join(cls.tmp, "profil.json")
        err = profil.save(iny_profil(), pfile)
        assert not err, err
        code = CHILD % {"here": HERE, "pfile": pfile, "db": os.path.join(cls.tmp, "tc.db"), "adb": os.path.join(cls.tmp, "a.db")}
        env = dict(os.environ, PYTHONIOENCODING="utf-8", TC_ESP="")
        env.pop("ENERGY_EXT_ID", None)
        env.pop("ENERGY_METER_IDS", None)
        r = subprocess.run([sys.executable, "-X", "utf8", "-c", code], capture_output=True, env=env, cwd=cls.tmp, timeout=120)
        if r.returncode:
            raise RuntimeError("podproces zlyhal:\n" + r.stderr.decode("utf-8", "replace"))
        cls.o = json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1])

    def test_mapy(self):
        o = self.o
        self.assertEqual(o["ROOMS"], ["obyvacka", "spalna", "loznica"])
        self.assertEqual(o["ROOM_NAMES"], {"obyvacka": "Obývačka", "spalna": "Spálňa", "loznica": "Ložnica hostí"})
        self.assertEqual(o["TARGET_ROOMS"], ["spalna", "loznica"])
        self.assertEqual(o["TARGET_DEFAULTS"], {"spalna": {"min": 20.0}, "loznica": {"min": 19.5}})
        self.assertEqual(o["S"]["obyvacka"], "Sledovaná teplota (Obývačka)")
        self.assertEqual(o["S"]["obyvacka_z"], "Izba Obývačka (Zigbee)")
        self.assertNotIn("detska", o["S"])
        self.assertNotIn("chyba", o["S"])
        self.assertNotIn("bojler", o["S"])

    def test_mantinely_podlahovka(self):
        lim = self.o["ALLOWED"]
        self.assertEqual(lim["FSV 2031 WL2 voda pri najchladnejšom"][:2], [30.0, 40.0])
        self.assertEqual(lim["FSV 2032 WL2 voda pri najteplejšom"][:2], [24.0, 32.0])
        self.assertEqual(lim["Maximálny kladný posun krivky"][:2], [0.5, 2.0])

    def test_read_now_bez_chyb_entit(self):
        names = self.o["READ_NOW"]
        for zle in ("", "Chybový kód TČ", "Teplota TÚV (Bojler)", "Cieľová teplota TÚV", "Ohrev TÚV", "Režim TÚV",
                    "Elektrický dohrev TÚV (Booster heater)", "3-cestný ventil (Smer ohrevu)", "Odmrazovanie"):
            self.assertNotIn(zle, names)
        self.assertIn("Sledovaná teplota (Obývačka)", names)
        self.assertIn("Vonkajšia teplota", names)

    def test_prompty(self):
        o = self.o
        s = o["SYSTEM"]
        self.assertIn("Samsung EHS Mono 8 kW", s)
        self.assertIn("podlahové kúrenie", s)
        self.assertIn("Obývačka", s)
        self.assertNotIn("Hlavice radiátorov", s)
        self.assertNotIn("priškrtil", s)
        self.assertNotIn("detsk", s.lower())
        self.assertIn("bez ohrevu TÚV", s)
        self.assertIn("obyvacka|spalna|loznica", o["CHAT"])
        self.assertNotIn("detska", o["CHAT"])
        self.assertIn("Obývačka", o["CHAT_USER"])
        self.assertEqual(o["ROOM_TARGET"], [20.0, 24.5])

    def test_zberac_elektrina_dhw(self):
        o = self.o
        self.assertEqual(set(o["REST_POLL"]), {"Izba Obývačka (Zigbee)", "Izba Spálňa", "Izba Ložnica", "Radiátor Test"})
        self.assertEqual(o["ENERGY"], ["ssd_ims:iny_dom", ["sensor.em_celkom"]])
        self.assertEqual(o["DHW"], ["", False])
        self.assertEqual(o["DHW_TICK"], "nečinný (zariadenie nemá TÚV)")
        self.assertFalse(o["CAN_START"][0])
        self.assertEqual(o["WRITE_ERRS"], [])
        self.assertEqual(o["GROUPS"], ["obyvacka", "spalna", "loznica", None, None])
        self.assertIn(["obyvack", "obyvacka"], o["KEYWORDS"])
        self.assertIn(["loznic", "loznica"], o["KEYWORDS"])

    def test_day_summary_referencna_izba(self):
        s = self.o["SUM"]
        self.assertEqual(s["ciel_izba"], 22.0)
        self.assertEqual(s["obyvacka"], 21.17)           # (10 h x 20 + 14 h x 22) / 24
        self.assertEqual(s["rozdiel_spalna"], -1.5)
        self.assertEqual(s["rozdiel_loznica"], round(19.0 - 21.17, 2))
        self.assertEqual((s["obyvacka_min"], s["obyvacka_max"]), (20.0, 22.0))
        self.assertEqual(s["hod_pod_cielom"], 10)          # 20 < 22 - 0.4
        self.assertEqual(s["hod_nad_cielom"], 0)
        self.assertIsNone(s["beh"])                        # bez entity frekvencie nič nepadlo
        self.assertFalse(self.o["HAS_DETSKA"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
