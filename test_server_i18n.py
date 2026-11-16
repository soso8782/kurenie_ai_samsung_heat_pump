#!/usr/bin/env python3
"""Testy prekladu textov zo servera (SPEC_server_i18n): i18n.py (tr, tn, req_lang, home_lang), kľúče `srv.*` v locales/sk.json a en.json,
slovenský výstup identický s doterajším, anglický výstup bez slovenskej diakritiky, jazyk v API (hlavička X-Lang) a pokyn jazyka v promptoch
agenta. Nič sa nepripája von (ESP, HA, OpenRouter sú mockované)."""
import glob, http.client, json, os, re, sys, tempfile, threading, time, unittest
from http.server import ThreadingHTTPServer
from unittest import mock

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import i18n

SK_CHARS = re.compile("[áäčďéíľĺňóôŕšťúýžÁČĎÉÍĽĹŇÓŔŠŤÚÝŽ]")
LOC = os.path.join(ROOT, "locales")


def load(lang):
    with open(os.path.join(LOC, lang + ".json"), encoding="utf-8") as f:
        return json.load(f)


def write_locale(d, lang, data):
    with open(os.path.join(d, lang + ".json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


# ---------------------------------------------------------------- i18n.py
class Core(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        write_locale(self.d, "sk", {"_meta": {"jazyk": "sk"}, "srv.t.a": "Ahoj {n} {x}", "srv.t.len_sk": "Len po slovensky",
                                    "srv.t.p.1": "{n} vec", "srv.t.p.2_4": "{n} veci", "srv.t.p.5": "{n} vecí", "srv.t.p.n": "{n} vecí"})
        write_locale(self.d, "en", {"_meta": {"jazyk": "en"}, "srv.t.a": "Hello {n} {x}",
                                    "srv.t.p.1": "{n} thing", "srv.t.p.2_4": "{n} things", "srv.t.p.5": "{n} things", "srv.t.p.n": "{n} things"})
        self._p = mock.patch.object(i18n, "LOC", self.d)
        self._p.start()
        i18n._cache.clear()

    def tearDown(self):
        self._p.stop()
        i18n._cache.clear()

    def test_tr_vars_and_fallback(self):
        self.assertEqual(i18n.tr("srv.t.a", "sk", n=3, x="!"), "Ahoj 3 !")
        self.assertEqual(i18n.tr("srv.t.a", "en", n=3, x="!"), "Hello 3 !")
        self.assertEqual(i18n.tr("srv.t.len_sk", "en"), "Len po slovensky")         # chýba v en -> sk
        self.assertEqual(i18n.tr("srv.t.neexistuje", "en"), "srv.t.neexistuje")      # chýba všade -> kľúč
        self.assertEqual(i18n.tr("srv.t.a", "de", n=1, x="y"), "Ahoj 1 y")           # neznámy jazyk -> sk
        self.assertEqual(i18n.tr("srv.t.a", "en", n=1), "Hello 1 {x}")               # chýbajúca premenná sa nechá
        self.assertEqual(i18n.tr("srv.t.a", n=2, x="z"), "Ahoj 2 z")                 # predvolený jazyk sk
        self.assertEqual(i18n.tr("srv.t.a", "../x", n=2, x="z"), "Ahoj 2 z")         # neplatný názov jazyka sa nikdy nečíta zo súboru

    def test_tn_plurals(self):
        sk = [i18n.tn("srv.t.p", n, "sk") for n in (1, 2, 4, 5, 0, 11)]
        self.assertEqual(sk, ["1 vec", "2 veci", "4 veci", "5 vecí", "0 vecí", "11 vecí"])
        en = [i18n.tn("srv.t.p", n, "en") for n in (1, 2, 5, 0)]
        self.assertEqual(en, ["1 thing", "2 things", "5 things", "0 things"])
        self.assertEqual(i18n.tn("srv.t.nic", 3, "en"), "srv.t.nic.n")                # chýba všade -> kľúč s príponou

    def test_reload_on_mtime_change(self):
        self.assertEqual(i18n.tr("srv.t.a", "sk", n=1, x=""), "Ahoj 1 ")
        p = os.path.join(self.d, "sk.json")
        write_locale(self.d, "sk", {"srv.t.a": "Nové {n}"})
        os.utime(p, (time.time() + 5, time.time() + 5))
        self.assertEqual(i18n.tr("srv.t.a", "sk", n=1), "Nové 1")

    def test_loaded_once_while_unchanged(self):
        i18n.tr("srv.t.a", "sk")
        with mock.patch("builtins.open", side_effect=AssertionError("súbor sa nemá čítať znova")):
            self.assertEqual(i18n.tr("srv.t.a", "sk", n=1, x=2), "Ahoj 1 2")

    def test_req_lang(self):
        self.assertEqual(i18n.req_lang({"X-Lang": "en"}), "en")
        self.assertEqual(i18n.req_lang({"X-Lang": "EN"}), "en")
        self.assertEqual(i18n.req_lang({"X-Lang": "en-GB"}), "en")
        self.assertEqual(i18n.req_lang({"X-Lang": "sk"}), "sk")
        self.assertEqual(i18n.req_lang({"X-Lang": "de"}), "sk")        # neznámy jazyk (nie je v locales/)
        self.assertEqual(i18n.req_lang({"X-Lang": "../"}), "sk")
        self.assertEqual(i18n.req_lang({}), "sk")
        self.assertEqual(i18n.req_lang(None), "sk")

    def test_home_lang(self):
        self.assertEqual(i18n.home_lang({"UI_LANG": "en"}), "en")
        self.assertEqual(i18n.home_lang({"UI_LANG": " EN "}), "en")
        self.assertEqual(i18n.home_lang({"UI_LANG": "xx"}), "sk")
        self.assertEqual(i18n.home_lang({}), "sk")
        self.assertEqual(i18n.home_lang(None), "sk")

    def test_new_language_by_file(self):
        write_locale(self.d, "cs", {"srv.t.a": "Ahoj {n}"})
        self.assertEqual(i18n.req_lang({"X-Lang": "cs"}), "cs")


# ---------------------------------------------------------------- kľúče
class Keys(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sk, cls.en = load("sk"), load("en")
        cls.srv = {k for k in cls.sk if k.startswith("srv.")}

    def test_same_keys_alphabetical(self):
        self.assertEqual({k for k in self.en if k.startswith("srv.")}, self.srv)
        self.assertGreater(len(self.srv), 400)
        for d in (self.sk, self.en):
            ks = list(d)
            self.assertEqual(ks[1:], sorted(ks[1:]))

    def test_same_placeholders_and_not_empty(self):
        var = re.compile(r"\{(\w+)\}")
        for k in self.srv:
            self.assertEqual(sorted(var.findall(self.sk[k])), sorted(var.findall(self.en[k])), k)
            self.assertTrue(self.sk[k] and self.en[k], k)

    def test_every_used_key_exists(self):
        used = {}
        for fn in glob.glob(os.path.join(ROOT, "*.py")):
            base = os.path.basename(fn)
            if base.startswith(("test_", "t_")) or base in ("ha_api.py",):
                continue
            with open(fn, encoding="utf-8-sig") as f:
                txt = f.read()
            for m in re.finditer(r"""["'](srv\.[a-z0-9_]+\.[a-z0-9_.]*[a-z0-9_])["']""", txt):
                used.setdefault(m.group(1), base)
        self.assertGreater(len(used), 300)
        # kľúč končiaci „_“ je predpona skladaná za behu (tr("srv.stats.m_" + kľúč)); jej úplnosť overuje test_dynamic_keys_complete
        missing = [(k, f) for k, f in sorted(used.items())
                   if k not in self.sk and not any(k + s in self.sk for s in (".1", ".2_4", ".5", ".n"))
                   and not (k.endswith("_") and any(x.startswith(k) for x in self.sk))]
        self.assertEqual(missing, [])

    def test_plurals_complete(self):
        for k in self.srv:
            for suf in (".2_4", ".n"):
                if k.endswith(suf):
                    for s in (".1", ".2_4", ".5", ".n"):
                        self.assertIn(k[:-len(suf)] + s, self.srv)

    def test_english_has_no_slovak_diacritics(self):
        bad = [(k, v) for k, v in self.en.items() if k.startswith("srv.") and SK_CHARS.search(v)]
        # výnimky: cudzie znaky v ukážkach/príkladoch, slovenské hodnoty „zima“/„leto“ (kľúč nastavenia) a ΔT
        allowed = {"srv.contacts.sezona", "srv.app.pouzivatel"}
        self.assertEqual([x for x in bad if x[0] not in allowed], [])

    def test_dynamic_keys_complete(self):
        import ha_pub, stats, temps
        for m in stats.METRICS:
            if m["group"] != "izby" or m["key"] in ("ciel", "vonku"):
                self.assertIn("srv.stats.m_" + m["key"], self.srv, m["key"])
        for key, *_ in ha_pub.SENSORS:
            self.assertIn("srv.ha_pub.n_" + key, self.srv)
        for k, _n in temps.KINDS:
            self.assertIn("srv.temps.kind_" + k, self.srv)
        for k in ("-1d", "-7d", "-14d", "-30d", "-1y", "-2y", "-3y"):
            self.assertIn("srv.stats.q_" + k.lstrip("-"), self.srv)
        for code in ("e911", "data", "esp", "zberac", "dohrev", "agent_chyba", "ha", "chyba_tc"):
            for f in ("title", "desc", "fix"):
                self.assertIn("srv.stats.seed_%s_%s" % (code, f), self.srv)
        for k in ("dnes", "vcera", "tyzden", "tyzdne2", "mesiac", "rok1", "rok2", "rok3"):
            self.assertIn("srv.stats.lbl_" + k, self.srv)
        for k in ("zodpoveda", "nezodpoveda", "hore", "dole"):
            self.assertIn("srv.interventions." + k, self.srv)
        for k in ("chodba", "kotolna", "tw2", "tw1", "wl"):
            self.assertIn("srv.vonku.n_" + k, self.srv)


# ---------------------------------------------------------------- funkcie: sk identické, en bez diakritiky
class Tmp(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cfg = {"AGENT_DB": os.path.join(self.d, "a.db"), "TC_DB": os.path.join(self.d, "tc.db"), "STATS_DB": os.path.join(self.d, "stats.db"),
                    "HA_TOKEN": "", "OPENROUTER_API_KEY": "", "HA_URL": "http://ha.invalid:8123", "ESP_URL": "http://esp.invalid"}

    def no_sk(self, text):
        self.assertIsNone(SK_CHARS.search(str(text)), text)


class Functions(Tmp):
    def test_stats_dates_and_metrics(self):
        import stats
        with self.assertRaises(ValueError) as e:
            stats._pdate("nonsense")
        self.assertEqual(str(e.exception), "Neplatný dátum „nonsense“ (očakávaný tvar RRRR-MM-DD).")
        with self.assertRaises(ValueError) as e:
            stats._pdate("nonsense", "en")
        self.assertEqual(str(e.exception), "Invalid date “nonsense” (expected format YYYY-MM-DD).")
        self.assertIs(stats.metrics("sk"), stats.METRICS)
        sk = {m["key"]: m["label"] for m in stats.metrics("sk")}
        en = {m["key"]: m["label"] for m in stats.metrics("en")}
        self.assertEqual(sk["tw2"], "Voda do domu (výstup)")
        self.assertEqual(en["tw2"], "Water to the house (outlet)")
        self.assertEqual(sk["kwh_cal"], "Spotreba (kalibrovaná) kWh")
        self.assertEqual(en["kwh_cal"], "Consumption (calibrated) kWh")
        self.assertEqual([m["key"] for m in stats.metrics("en")], [m["key"] for m in stats.METRICS])
        self.assertTrue(any(v.endswith(" (controlling)") for v in en.values()))
        self.assertTrue(any(v.endswith(" (riadiaca)") for v in sk.values()))

    def test_stats_quick_and_codes(self):
        import stats
        s = {x["key"]: x["label"] for x in stats.quick_dates(self.cfg, "2026-11-15")}
        self.assertEqual(s["-1d"], "Včera"); self.assertEqual(s["-14d"], "Pred 2 týždňami"); self.assertEqual(s["-3y"], "Pred 3 rokmi")
        e = {x["key"]: x["label"] for x in stats.quick_dates(self.cfg, "2026-11-15", lang="en")}
        self.assertEqual(e["-1d"], "Yesterday"); self.assertEqual(e["-14d"], "2 weeks ago"); self.assertEqual(e["-3y"], "3 years ago")
        codes_sk = {c["code"]: c for c in stats.fault_codes_list(self.cfg)}
        codes_en = {c["code"]: c for c in stats.fault_codes_list(self.cfg, "en")}
        self.assertEqual(codes_sk["E911"]["title"], "Nízky prietok vody")
        self.assertEqual(codes_en["E911"]["title"], "Low water flow")
        for c in codes_en.values():
            self.no_sk(c["title"] + c["description"] + c["fix"])
        # kód upravený používateľom sa nepreloží
        stats.fault_code_set(self.cfg, "E911", "Môj názov", "Môj popis", "Moja oprava")
        self.assertEqual({c["code"]: c for c in stats.fault_codes_list(self.cfg, "en")}["E911"]["title"], "Môj názov")

    def test_stats_faults_auto_texts(self):
        import stats
        row = (1, 1000, 1600, "DATA", "gap", "Výpadok dát", "Výpadok dát trval 10 min.", "gap:1", 1000)
        fc = {}
        self.assertEqual(stats._fault_dict(row, fc, 2000)["text"], "Výpadok dát trval 10 min.")
        self.assertEqual(stats._fault_dict(row, fc, 2000, "en")["text"], "Data outage lasted 10 min.")
        row2 = (2, 1000, None, "E15", "tc_error", "", "Chybový kód tepelného čerpadla E15.", "tc:1", 1000)
        self.assertEqual(stats._fault_dict(row2, fc, 2000, "en")["text"], "Heat pump error code E15.")
        self.assertEqual(stats._fault_dict(row2, fc, 2000)["title"], "Neznáma chyba E15")
        self.assertEqual(stats._fault_dict(row2, fc, 2000, "en")["title"], "Unknown error E15")
        self.no_sk(stats._fault_dict(row2, fc, 2000, "en")["description"])

    def test_spotreba_vonku_errors(self):
        import spotreba, vonku
        with self.assertRaises(ValueError) as e:
            spotreba.report(self.cfg, "nonsense", None)
        self.assertEqual(str(e.exception), "Neplatný dátum „nonsense“ (očakávaný tvar RRRR-MM-DD).")
        with self.assertRaises(ValueError) as e:
            spotreba.report(self.cfg, "nonsense", None, lang="en")
        self.assertEqual(str(e.exception), "Invalid date “nonsense” (expected format YYYY-MM-DD).")
        with self.assertRaises(ValueError) as e:
            vonku.report(self.cfg, krok=7)
        self.assertEqual(str(e.exception), "Krok musí byť 1, 2, 3 alebo 5 °C.")
        with self.assertRaises(ValueError) as e:
            vonku.report(self.cfg, krok=7, lang="en")
        self.assertEqual(str(e.exception), "The step must be 1, 2, 3 or 5 °C.")
        with self.assertRaises(ValueError) as e:
            vonku.kurenie(self.cfg, od="2026-11-05", do="2026-11-03", lang="en")
        self.no_sk(e.exception)
        self.assertEqual(vonku._nazov("tw2", "sk"), "Voda výstup")
        self.assertEqual(vonku._nazov("tw2", "en"), "Water outlet")

    def test_app_validators(self):
        import app
        self.assertEqual(app.access_config({}, {"team_domain": 5}), (400, {"error": "Neplatné hodnoty."}))
        self.assertEqual(app.access_config({}, {"team_domain": 5}, "en"), (400, {"error": "Invalid values."}))
        self.assertEqual(app.save_settings(self.cfg, {})[1], {"error": "Nič na uloženie."})
        self.assertEqual(app.save_settings(self.cfg, {}, "en")[1], {"error": "Nothing to save."})
        self.assertEqual(app.save_settings(self.cfg, {"api_key": "abc"})[1], {"error": "Kľúč nevyzerá ako platný kľúč OpenRouter (začína sk-or-…)."})
        self.assertEqual(app.save_settings(self.cfg, {"interval": 7}, "en")[1], {"error": "Invalid interval."})
        self.assertEqual(app.save_settings(self.cfg, {"ui_lang": "de"}, "en")[1], {"error": "Invalid language."})
        self.assertEqual(app.mqtt_validate({"host": ""}, {})[0], "Adresa brokera musí byť IP adresa alebo názov počítača (1 až 253 znakov, bez medzier).")
        self.assertEqual(app.mqtt_validate({"host": "h", "port": "99999"}, {})[0], "Port musí byť číslo od 1 do 65535.")
        self.assertEqual(app.mqtt_validate({"host": "h", "port": "99999"}, {}, "en")[0], "The port must be a number from 1 to 65535.")
        self.assertEqual(app.series_get(self.cfg, {"k": ["x"]})[1], {"error": "Neznámy kľúč série."})
        self.assertEqual(app.series_get(self.cfg, {"k": ["x"]}, "en")[1], {"error": "Unknown series key."})
        self.assertEqual(app.tuv_boost_post(self.cfg, {"action": "x"}, "w")[1], {"error": "Neplatná akcia."})
        self.assertEqual(app.tuv_boost_post(self.cfg, {"action": "x"}, "w", "en")[1], {"error": "Invalid action."})
        self.assertEqual(app.targets_set(self.cfg, {}, "en")[1], {"error": "The list of desired temperatures is missing."})
        self.assertEqual(app.targets_set(self.cfg, {})[1], {"error": "Chýba zoznam želaných teplôt."})
        with self.assertRaises(ValueError) as e:
            app.stats_period({"from": ["2026-02-01"], "to": ["2026-01-01"]})
        self.assertEqual(str(e.exception), "Dátum od nesmie byť neskôr ako dátum do.")
        with self.assertRaises(ValueError) as e:
            app.stats_period({"from": ["2026-02-01"], "to": ["2026-01-01"]}, "en")
        self.assertEqual(str(e.exception), "The from date must not be later than the to date.")
        self.assertEqual(app.profil_post({}, "en")[1]["error"], "The profile is missing.")
        self.assertEqual(app.profil_post({})[1]["chyby"], ["Telo požiadavky musí obsahovať objekt „profil“."])

    def test_modules_errors_sk_and_en(self):
        import abtest, cftunnel, contacts, dhw, notify, profil, temps, agent
        from common import adb
        c = adb(self.cfg)
        try:
            self.assertEqual(cftunnel.norm_email(" A@B.sk "), "a@b.sk")
            for fn, sk_txt, en_txt in (
                    (lambda lg: cftunnel.norm_email("x", lg), "Neplatný e-mail.", "Invalid e-mail."),
                    (lambda lg: contacts.group_save(c, "", lang=lg), "Názov skupiny musí mať 1 až 40 znakov.", "The group name must be 1 to 40 characters long."),
                    (lambda lg: contacts.update_contact(c, "binary_sensor.nie", lang=lg), "Neznámy senzor.", "Unknown sensor."),
                    (lambda lg: temps.update_sensor(c, "sensor.nie", lang=lg), "Neznáme čidlo.", "Unknown sensor."),
                    (lambda lg: temps.save_settings(c, {"notify_min": "x"}, lg), "Čas upozornenia musí byť celé číslo minút.", "The notification time must be a whole number of minutes."),
                    (lambda lg: notify.validate_subscription("x", lg), "Chýba predplatné zariadenia.", "The device subscription is missing."),
                    (lambda lg: agent.parse_room_target("abc", lg), "Neplatná teplota.", "Invalid temperature."),
                    (lambda lg: agent.parse_room_target(10, lg), "Cieľová teplota musí byť 20,0 až 24,5 °C v kroku 0,5.", "The target temperature must be 20.0 to 24.5 °C in steps of 0.5."),
            ):
                for lg, want in (("sk", sk_txt), ("en", en_txt)):
                    with self.assertRaises(ValueError) as e:
                        fn(lg)
                    self.assertEqual(str(e.exception), want)
            self.assertEqual(abtest.start(self.cfg, c, 5, lang="en"), (400, {"error": "The test length must be 6, 8, 10 or 14 days."}))
            self.assertEqual(abtest.start(self.cfg, c, 5)[1], {"error": "Dĺžka testu musí byť 6, 8, 10 alebo 14 dní."})
            self.assertEqual(dhw.manual_start(self.cfg, c, 10, "w"), (False, "Teplota musí byť 45 až 52 °C po 1 °C."))
            self.assertEqual(dhw.manual_start(self.cfg, c, 10, "w", lang="en"), (False, "The temperature must be 45 to 52 °C in steps of 1 °C."))
            self.assertEqual(dhw.manual_stop(self.cfg, c, "w"), (False, "Manuálny ohrev nebeží."))
            self.assertEqual(dhw.manual_stop(self.cfg, c, "w", "en"), (False, "Manual heating is not running."))
            self.assertEqual(dhw.set_target_manual(self.cfg, c, "x", "w"), (False, "Cieľ TÚV musí byť celé číslo od 40 do 52 °C."))
            self.assertEqual(dhw.set_target_manual(self.cfg, c, "x", "w", "en"), (False, "The DHW target must be a whole number from 40 to 52 °C."))
        finally:
            c.close()
        e_sk = profil.validate({"zariadenie": {"fazy": 2}})[1]
        e_en = profil.validate({"zariadenie": {"fazy": 2}}, "en")[1]
        self.assertEqual((e_sk, e_en), (["zariadenie.fazy: 1 alebo 3."], ["zariadenie.fazy: 1 or 3."]))

    def test_cftunnel_call_errors(self):
        import cftunnel
        with mock.patch("urllib.request.urlopen", side_effect=OSError("boom")):
            with self.assertRaises(cftunnel.CfError) as e:
                cftunnel.call("t", "GET", "/x")
            self.assertEqual(str(e.exception), "Cloudflare neodpovedá: boom")
            with self.assertRaises(cftunnel.CfError) as e:
                cftunnel.call("t", "GET", "/x", lang="en")
            self.assertEqual(str(e.exception), "Cloudflare is not responding: boom")
            with self.assertRaises(cftunnel.CfError) as e:
                cftunnel.setup("zly", "kurenie.example.com", "a@b.sk", False, [], "en")
            self.assertEqual(str(e.exception), "The API token does not look right.")

    def test_interventions_verdict(self):
        import interventions
        iv = {"room": None, "text": ""}
        delta = {"diff": {}, "rooms": {}, "kwh_na_stupen_pct": None, "vonku": None, "okna_min": None}
        ref = interventions.REF_NAME
        sk = interventions.verdict(iv, {}, {}, delta)
        self.assertEqual(sk, "Rozdiely izieb voči referenčnej izbe (%s) sa nedali porovnať (chýbajú dáta). "
                             "Spotreba na 1 °C sa nedá porovnať (málo studený deň alebo chýbajú dáta)." % ref)
        en = interventions.verdict(iv, {}, {}, delta, "en")
        self.assertEqual(en, "The room differences relative to the reference room (%s) could not be compared (data is missing). "
                             "Consumption per 1 °C cannot be compared (not enough cold days or data is missing)." % ref)
        d2 = {"diff": {"obyvacka": 0.4}, "rooms": {}, "kwh_na_stupen_pct": -12.0, "vonku": None, "okna_min": None}
        b = {"diff": {"obyvacka": 0.1}, "kwh_na_stupen": 1.5}
        a = {"diff": {"obyvacka": 0.5}, "kwh_na_stupen": 1.3, "dt_beh": None}
        self.assertIn("rozdiel +0,1 → +0,5 °C (+0,4)", interventions.verdict(iv, b, a, d2))
        self.assertIn("difference +0.1 → +0.5 °C (+0.4)", interventions.verdict(iv, b, a, d2, "en"))
        self.assertIn("Consumption per 1 °C changed by -12 % (1.50 → 1.30 kWh/°C).", interventions.verdict(iv, b, a, d2, "en"))

    def test_abtest_verdict(self):
        import abtest
        res = {"A": {"n": 1}, "B": {"n": 0}, "uspora_pct": None, "neistota_pct": None}
        self.assertEqual(abtest.verdict(res, False), "Zatiaľ nie je dosť platných dní na porovnanie (dni A: 1, dni B: 0). "
                         "Treba aspoň jeden platný deň z každého režimu, na spoľahlivý odhad aspoň dva.")
        self.no_sk(abtest.verdict(res, False, "en"))

    def test_notify_texts(self):
        import notify
        got = []
        with mock.patch.object(notify, "event", lambda cfg, kind, title, msg: got.append((kind, title, msg)) or (True, None)):
            notify.target_changed({}, 21.0, 22.5, "ja@x.sk")
            notify.target_changed({"UI_LANG": "en"}, 21.0, 22.5, "ja@x.sk")
        self.assertEqual(got[0], ("cielova_zmena", "Kúrenie – cieľová teplota", "Cieľová teplota zmenená na 22,5 °C (ja@x.sk)"))
        self.assertEqual(got[1], ("cielova_zmena", "Heating – target temperature", "Target temperature changed to 22.5 °C (ja@x.sk)"))
        self.assertEqual(notify.err_text("nenastavené", "en"), "not configured")
        self.assertEqual(notify.err_text("nenastavené", "sk"), "nenastavené")
        self.assertEqual(notify.err_text("iný text", "en"), "iný text")

    def test_contacts_notifications_language(self):
        import contacts
        from common import adb
        c = adb(self.cfg)
        sent = []
        try:
            contacts.ensure(c)
            c.execute("INSERT INTO contacts(entity_id, ha_name, alias, group_id, enabled, device_class, state, since, area, note, first_seen, last_seen) "
                      "VALUES('binary_sensor.k_contact','Kuchyna',NULL,?,1,'window','on',?,NULL,NULL,1,1)", (contacts.other_id(c), int(time.time()) - 3600))
            c.commit()
            for cfg in ({}, {"UI_LANG": "en"}):
                c.execute("DELETE FROM state WHERE k='window_notified'"); c.commit()
                with mock.patch.object(contacts, "_send", lambda cfg, t, m: sent.append((t, m)) or True), \
                        mock.patch.object(contacts, "_outside_temp", lambda cfg: -2.5):
                    contacts.check_notifications(dict(cfg), c, int(time.time()))
        finally:
            c.close()
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0][0], "Kúrenie: otvorené okno")
        self.assertIn("Vonku je -2,5 °C.", sent[0][1])
        self.assertEqual(sent[1][0], "Heating: open window")
        self.assertIn("It is -2.5 °C outside.", sent[1][1])
        self.assertTrue(sent[1][1].startswith("Open window: "))

    def test_ha_pub_discovery(self):
        import ha_pub
        sk, en = ha_pub.discovery(), ha_pub.discovery("en")
        self.assertEqual(len(sk), len(en))
        for (t1, j1), (t2, j2) in zip(sk, en):
            d1, d2 = json.loads(j1), json.loads(j2)
            self.assertEqual(t1, t2)
            self.assertEqual(d1["unique_id"], d2["unique_id"])
            self.assertEqual(d1["object_id"], d2["object_id"])
            self.assertEqual(d1["device"]["identifiers"], d2["device"]["identifiers"])
            self.no_sk(d2["name"])
        self.assertEqual(json.loads(sk[0][1])["name"], "Príkon")
        self.assertEqual(json.loads(en[0][1])["name"], "Power")
        self.assertEqual(json.loads(sk[0][1])["device"]["name"], "Kúrenie – tepelné čerpadlo")
        self.assertEqual(json.loads(en[0][1])["device"]["name"], "Heating – heat pump")

    def test_dhw_notification_and_tick_texts(self):
        import dhw
        self.assertEqual(dhw._why("koniec okna", "en"), "the window ended")
        self.assertEqual(dhw._why("koniec okna", "sk"), "koniec okna")


# ---------------------------------------------------------------- agent: pokyn jazyka v promptoch, texty bez modelu
class AgentTests(Tmp):
    def test_language_note(self):
        import agent
        self.assertEqual(agent.lang_note("en"), "\n\nOdpovedaj v jazyku: English")
        self.assertEqual(agent.lang_note("sk"), "\n\nOdpovedaj v jazyku: slovenčina")
        for p in (agent.SYSTEM, agent.LEARN_SYSTEM, agent.CHAT_SYSTEM, agent.CHAT_SYSTEM_USER, agent.WEEKLY_SYSTEM):
            w = agent.with_lang(p, "en")
            self.assertTrue(w.startswith(p) and w.endswith("Odpovedaj v jazyku: English"))
            self.assertIn("po slovensky", p)      # samotné prompty ostávajú po slovensky

    def _captured_system(self, role, ui_lang):
        import agent
        from common import adb
        cfg = dict(self.cfg, OPENROUTER_API_KEY="k", UI_LANG=ui_lang)
        seen = []

        def fake_llm(cfg_, system, user, *a, **k):
            seen.append(system)
            return json.dumps({"odpoved": "ok", "ciel": None}), 1, 1, 0.0
        c = adb(cfg)
        try:
            with mock.patch.object(agent, "call_llm", fake_llm), mock.patch.object(agent, "user_context", lambda *a: "ctx"), \
                    mock.patch.object(agent, "build_input", lambda *a, **k: ("ctx", {})):
                agent.chat_reply(cfg, c, "ahoj", role=role, email="u@x.sk")
        finally:
            c.close()
        return seen[0]

    def test_chat_prompts_get_language_note(self):
        import agent
        for role, base in (("user", agent.CHAT_SYSTEM_USER.replace("{email}", "u@x.sk")), ("admin", agent.CHAT_SYSTEM)):
            en = self._captured_system(role, "en")
            sk = self._captured_system(role, "sk")
            self.assertEqual(en, base + "\n\nOdpovedaj v jazyku: English")
            self.assertEqual(sk, base + "\n\nOdpovedaj v jazyku: slovenčina")

    def test_chat_local_texts_by_request_language(self):
        import agent
        from common import adb
        c = adb(self.cfg)
        try:
            r_sk = agent.chat_reply(dict(self.cfg), c, "ahoj", role="admin")
            r_en = agent.chat_reply(dict(self.cfg), c, "hello", role="admin", lang="en")
            u_en = agent.chat_reply(dict(self.cfg), c, "hello", role="user", email="u@x.sk", lang="en")
        finally:
            c.close()
        self.assertEqual(r_sk[0], "Zapísané. Bez kľúča OpenRouter ti neviem odpovedať, ale správu zohľadním, hneď ako bude kľúč nastavený.")
        self.assertEqual(r_en[0], "Noted. Without an OpenRouter key I cannot reply, but I will take the message into account as soon as the key is set.")
        self.assertEqual(u_en[0], "I noted the message. Without an OpenRouter key I cannot reply or change the temperature; ask the administrator.")

    def test_log_rutina_language(self):
        import agent
        from common import adb
        c = adb(self.cfg)
        h = {"ts": 1000, "n": 5, agent.REF_ROOM: 23.4, "vonku": 3.5, "dt_beh": 4.8, "beh": 40}
        try:
            r_sk = agent.log_rutina(dict(self.cfg), c, "advise", {}, [h], [], 4600)
            r_en = agent.log_rutina(dict(self.cfg, UI_LANG="en"), c, "advise", {}, [h], [], 4600)
        finally:
            c.close()
        self.assertEqual(r_sk["summary"], "Bez zmien: %s 23,4 °C (cieľ –), vonku 3,5 °C, ΔT 4,8 °C, kompresor 40 %% hodiny. Model nebolo treba volať." % agent.REF_NAME.lower())
        self.assertEqual(r_en["summary"], "No changes: %s 23.4 °C (target –), outdoors 3.5 °C, ΔT 4.8 °C, compressor 40 %% of the hour. No need to call the model." % agent.REF_NAME.lower())

    def test_undo_texts(self):
        import agent
        from common import adb
        c = adb(self.cfg)
        try:
            self.assertEqual(agent.undo(self.cfg, c, "x"), (False, "Neplatné číslo akcie."))
            self.assertEqual(agent.undo(self.cfg, c, "x", "en"), (False, "Invalid action number."))
            self.assertEqual(agent.undo(self.cfg, c, 999999, "en"), (False, "The action does not exist."))
        finally:
            c.close()

    def test_chat_room_target_texts(self):
        import agent
        from common import adb
        c = adb(self.cfg)
        try:
            self.assertEqual(agent.chat_room_target(self.cfg, c, "abc", "u", 1000), (False, "Zmenu teploty som nevykonal: nerozumiem požadovanej hodnote."))
            ok, text = agent.chat_room_target(self.cfg, c, 30, "u", 1000, "en")
            self.assertFalse(ok)
            self.assertEqual(text, "I did not change the temperature: the allowed range is 20.0 to 24.5 °C in steps of 0.5 (requested 30.00).")
            ok, text = agent.chat_room_target(self.cfg, c, 30, "u", 1000)
            self.assertEqual(text, "Zmenu teploty som nevykonal: povolený rozsah je 20,0 až 24,5 °C v kroku 0,5 (požadované 30,00).")
        finally:
            c.close()


# ---------------------------------------------------------------- HTTP API: hlavička X-Lang
class Http(Tmp):
    def setUp(self):
        super().setUp()
        import app, profil
        self.app = app
        self.pf = os.path.join(self.d, "profil.json")
        self.ps = [mock.patch.object(app, "load_cfg", lambda: dict(self.cfg)),
                   mock.patch.object(app.H, "identity", lambda s, cfg: ("admin", "a@b.sk")),
                   mock.patch.object(profil, "PROFILE_FILE", self.pf)]
        for p in self.ps:
            p.start()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close()
        for p in self.ps:
            p.stop()

    def req(self, method, path, body=None, lang=None):
        h = {"X-Kurenie": "1"}
        if lang:
            h["X-Lang"] = lang
        raw = None
        if body is not None:
            raw = json.dumps(body); h["Content-Type"] = "application/json"
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=15)
        c.request(method, path, raw, h)
        r = c.getresponse(); data = r.read(); c.close()
        return r.status, (json.loads(data.decode("utf-8")) if data[:1] in (b"{", b"[") else data.decode("utf-8"))

    def test_spotreba_invalid_date(self):
        s, o = self.req("GET", "/api/spotreba?od=nonsense")
        self.assertEqual((s, o), (400, {"error": "Neplatný dátum „nonsense“ (očakávaný tvar RRRR-MM-DD)."}))
        s, o = self.req("GET", "/api/spotreba?od=nonsense", lang="sk")
        self.assertEqual(o["error"], "Neplatný dátum „nonsense“ (očakávaný tvar RRRR-MM-DD).")
        s, o = self.req("GET", "/api/spotreba?od=nonsense", lang="en")
        self.assertEqual((s, o), (400, {"error": "Invalid date “nonsense” (expected format YYYY-MM-DD)."}))
        self.no_sk(o["error"].replace("“", "").replace("”", ""))
        s, o = self.req("GET", "/api/spotreba?od=nonsense", lang="de")        # neznámy jazyk -> sk
        self.assertIn("Neplatný dátum", o["error"])

    def test_vonku_and_akokuri_errors(self):
        s, o = self.req("GET", "/api/vonku?krok=9", lang="en")
        self.assertEqual((s, o["error"]), (400, "The step must be 1, 2, 3 or 5 °C."))
        s, o = self.req("GET", "/api/akokuri?krok=9")
        self.assertEqual((s, o["error"]), (400, "Krok musí byť 1, 2 alebo 5 °C."))

    def test_profil_post_error(self):
        import profil
        bad = profil.default(); bad["zariadenie"]["vykon_kw"] = 99
        s, o = self.req("POST", "/api/profil", {"profil": bad}, lang="en")
        self.assertEqual(s, 400)
        self.assertEqual(o["error"], "The profile contains errors (1).")
        self.assertEqual(o["chyby"], ["zariadenie.vykon_kw: a number 2–30."])
        s, o = self.req("POST", "/api/profil", {"profil": bad})
        self.assertEqual((o["error"], o["chyby"]), ("Profil obsahuje chyby (1).", ["zariadenie.vykon_kw: číslo 2–30."]))
        s, o = self.req("POST", "/api/profil", {}, lang="en")
        self.assertEqual(o, {"error": "The profile is missing.", "chyby": ["The request body must contain a “profil” object."]})
        self.assertFalse(os.path.exists(self.pf))

    def test_settings_errors_and_ui_lang(self):
        s, o = self.req("POST", "/api/settings", {}, lang="en")
        self.assertEqual(o, {"error": "Nothing to save."})
        s, o = self.req("POST", "/api/settings", {"ui_lang": "de"}, lang="en")
        self.assertEqual((s, o), (400, {"error": "Invalid language."}))
        with mock.patch.object(self.app, "save_cfg") as sv:
            s, o = self.req("POST", "/api/settings", {"ui_lang": "en"}, lang="sk")
        self.assertEqual((s, o), (200, {"ok": True}))
        sv.assert_called_once_with({"UI_LANG": "en"})
        from common import adb
        c = adb(self.cfg)
        try:
            last = c.execute("SELECT summary FROM runs ORDER BY ts DESC, id DESC LIMIT 1").fetchone()[0]
        finally:
            c.close()
        self.assertEqual(last, "Nastavenia zmenené cez web: jazyk upozornení a agenta en")     # záznam v jazyku domácnosti (ešte sk)

    def test_stats_api_language(self):
        s, o = self.req("GET", "/api/stats/meta", lang="en")
        self.assertEqual(s, 200)
        lab = {m["key"]: m["label"] for m in o["metrics"]}
        self.assertEqual(lab["tw2"], "Water to the house (outlet)")
        self.no_sk(o["hdd_note"])
        s, o = self.req("GET", "/api/stats/meta")
        lab = {m["key"]: m["label"] for m in o["metrics"]}
        self.assertEqual(lab["tw2"], "Voda do domu (výstup)")
        self.assertEqual(o["hdd_note"], "Stupňo-dni = súčet max(0, základ − vonkajšia teplota) za hodiny s dátami, delený 24.")
        s, o = self.req("GET", "/api/stats/quick?base=2026-11-15", lang="en")
        self.assertEqual({x["key"]: x["label"] for x in o["items"]}["-7d"], "A week ago")
        s, o = self.req("GET", "/api/stats/range?from=bad", lang="en")
        self.assertEqual((s, o["error"]), (400, "Invalid date in parameter from (expected YYYY-MM-DD)."))
        s, o = self.req("GET", "/api/stats/range?from=bad")
        self.assertEqual(o["error"], "Neplatný dátum v parametri from (očakávam RRRR-MM-DD).")
        s, o = self.req("POST", "/api/stats/fault", {"ts_start": "zle"}, lang="en")
        self.assertEqual((s, o["error"]), (400, "Invalid fault time (expected unix seconds or YYYY-MM-DD HH:MM)."))
        s, o = self.req("GET", "/api/stats/export?kind=compare&dates=2026-11-14,2026-11-15&lang=en", lang="sk")
        self.assertEqual(s, 200)
        self.assertIn("Date;Label;Hour;", o)

    def test_misc_errors(self):
        s, o = self.req("GET", "/api/series?k=x", lang="en")
        self.assertEqual(o, {"error": "Unknown series key."})
        s, o = self.req("POST", "/api/tuv_boost", {"action": "x"}, lang="en")
        self.assertEqual(o, {"error": "Invalid action."})
        s, o = self.req("POST", "/api/targets", {}, lang="en")
        self.assertEqual(o, {"error": "The list of desired temperatures is missing."})
        s, o = self.req("POST", "/api/intervention", {"text": ""}, lang="en")
        self.assertEqual(o, {"error": "The intervention description is empty or longer than 500 characters."})
        s, o = self.req("POST", "/api/abtest_stop", {}, lang="en")
        self.assertEqual(o, {"error": "No test is running."})
        s, o = self.req("POST", "/api/abtest_start", {"days": 3}, lang="en")
        self.assertEqual(o, {"error": "The test length must be 6, 8, 10 or 14 days."})
        s, o = self.req("POST", "/api/abtest_start", {"days": 3})
        self.assertEqual(o, {"error": "Dĺžka testu musí byť 6, 8, 10 alebo 14 dní."})
        s, o = self.req("POST", "/api/message", {"text": ""}, lang="en")
        self.assertEqual(o, {"error": "The message is empty or longer than 2000 characters."})
        s, o = self.req("POST", "/api/notify_test", {}, lang="en")
        self.assertEqual(s, 200)
        self.no_sk(o["error"] or "")

    def test_no_slovak_in_english_errors_sample(self):
        samples = [("GET", "/api/series?k=x", None), ("POST", "/api/tuv_target", {}), ("POST", "/api/contacts/sensor", {}),
                   ("POST", "/api/temps/sensor", {}), ("POST", "/api/access_config", {"team_domain": "zle"}), ("POST", "/api/mqtt_test", {"host": ""}),
                   ("POST", "/api/users", {"email": "x", "role": "admin"}), ("POST", "/api/room_target", {"value": "x"}),
                   ("POST", "/api/undo", {"action_id": "x"}), ("POST", "/api/profil/predvoleny", {})]
        for m, p, b in samples:
            s, o = self.req(m, p, b, lang="en")
            txt = json.dumps(o, ensure_ascii=False)
            self.assertIsNone(SK_CHARS.search(txt.replace("“", "").replace("”", "")), "%s %s -> %s" % (m, p, txt))


if __name__ == "__main__":
    unittest.main()
