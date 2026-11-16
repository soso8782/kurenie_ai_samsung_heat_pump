#!/usr/bin/env python3
"""Testy jazykov webu (SPEC_i18n): locales/sk.json a en.json, použité kľúče v ui.html a stats_ui.js, route /locales/xx.json
a heuristika, že v ui.html neostal natvrdo slovenský text. Nič sa nepripája von."""
import http.client, json, os, re, sys, threading, unittest
from http.server import ThreadingHTTPServer
from unittest import mock

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import app

LOC = os.path.join(ROOT, "locales")
PLURAL = (".1", ".2_4", ".5", ".n")
PREFIXES = ("nav", "top", "prehlad", "spotreba", "izby", "vonku", "tuv", "den", "cidla", "analyzy", "nastavenia", "zariadenie", "agent",
            "login", "spolocne", "cas", "stats", "srv")
KEY_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")
# kľúče skladané za behu ("cas.mes" + číslo mesiaca, "cas.dt_" + deň v týždni): v kóde je len predpona a za ňou `+`
# šablóny dátumov majú v jazykoch rôzne premenné ({m} v sk, {mon} v en)
VARS_EXEMPT = re.compile(r"^(cas\.dm|cas\.dm_kratko|cas\.dmy|stats\.datum\..*)$")
SK_CHARS = re.compile("[áäčďéíľĺňóôŕšťúýžÁČĎÉÍĽĹŇÓŔŠŤÚÝŽ]")

# Výnimky pre heuristiku „neostal slovenský reťazec“. Každá je zdôvodnená: ide o dáta, nie o text rozhrania.
ALLOW_LITERALS = {
    # názvy entít z ESP/zberača: sú to kľúče do status.current, nie texty pre používateľa
    "Sledovaná teplota (Detská izba)", "Aktuálna cieľová teplota izby", "Vonkajšia teplota", "Výstupná teplota vody (Tw2)",
    "Cieľová ekvitermická voda (WL)", "Delta T vykurovacej vody", "Frekvencia kompresora", "Otáčky obehového čerpadla (PWM)",
    "FSV 2031 WL2 voda pri najchladnejšom", "FSV 2032 WL2 voda pri najteplejšom", "Posun ekvitermickej krivky", "Teplota TÚV (Bojler)",
    "Cieľová teplota TÚV", "Denná spotreba TČ", "Chybový kód TČ",
    "Izba Jedáleň", "Izba Kuchyňa", "Izba Obývačka", "Izba Spálňa", "Izba Pracovňa", "Izba Kúpeľňa",
    # predvolené názvy izieb (názvy izieb pochádzajú z profilu a neprekladajú sa; toto je len záloha pred načítaním profilu)
    "Detská", "Jedáleň", "Kuchyňa", "Obývačka", "Spálňa", "Pracovňa", "Kúpeľňa",
    # skratka dňa v týždni z backendu (spotreba.py) použitá ako kľúč do cas.dt_*
    "št",
}
ALLOW_HTML = {
    "Kúrenie",        # <title> a meta pre PWA: za behu ich prepíše spolocne.nazov_app
    "Detská",         # #tgrefnote: názov izby z profilu, nastavuje ho JS
    "detskej izby",   # #abref a #setref: počiatočná hodnota, hneď prepísaná cez refGen2()
}


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def load_lang(lang):
    """locales/<lang>.json + (ak existuje) locales/stats.<lang>.json, kým ich nezlúči do hlavného súboru"""
    out, order = {}, None
    with open(os.path.join(LOC, lang + ".json"), encoding="utf-8") as f:
        d = json.load(f)
    order = list(d)
    out.update(d)
    sp = os.path.join(LOC, "stats.%s.json" % lang)
    if os.path.isfile(sp):
        with open(sp, encoding="utf-8") as f:
            for k, v in json.load(f).items():
                out.setdefault(k, v)
    return out, order


def base_of(key):
    for s in PLURAL:
        if key.endswith(s):
            return key[:-len(s)]
    return None


class Files(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sk, cls.sk_order = load_lang("sk")
        cls.en, cls.en_order = load_lang("en")

    def test_meta(self):
        for lang, d in (("sk", self.sk), ("en", self.en)):
            m = d.get("_meta")
            self.assertIsInstance(m, dict, lang)
            for k in ("jazyk", "nazov", "locale", "desatinna"):
                self.assertTrue(m.get(k), "%s: _meta.%s" % (lang, k))
            self.assertEqual(m["jazyk"], lang)
        self.assertEqual(self.sk["_meta"]["desatinna"], ",")
        self.assertEqual(self.en["_meta"]["desatinna"], ".")

    def test_meta_first_and_sorted(self):
        for lang in ("sk", "en"):
            order = load_lang(lang)[1]
            self.assertEqual(order[0], "_meta")
            self.assertEqual(order[1:], sorted(order[1:]), "%s.json nie je zoradený abecedne" % lang)

    def test_same_keys(self):
        a, b = set(self.sk), set(self.en)
        self.assertEqual(sorted(a - b), [], "kľúče chýbajú v en.json")
        self.assertEqual(sorted(b - a), [], "kľúče chýbajú v sk.json")

    def test_key_format(self):
        for k in self.sk:
            if k == "_meta":
                continue
            self.assertRegex(k, KEY_RE)
            self.assertIn(k.split(".")[0], PREFIXES, k)
            self.assertIsInstance(self.sk[k], str, k)
            self.assertIsInstance(self.en[k], str, k)
            self.assertTrue(self.sk[k] != "" or k.endswith(".sk_prazdne"), "prázdny text: " + k)

    def test_plurals_complete(self):
        # množné číslo = základ s príponou .2_4 alebo .n (".1" a ".5" môžu byť aj index, napr. stats.mesiac.5 = máj)
        bases = {base_of(k) for k in self.sk if base_of(k) and k.endswith((".2_4", ".n"))}
        for lang, d in (("sk", self.sk), ("en", self.en)):
            for b in bases:
                for s in PLURAL:
                    self.assertTrue(b + s in d, "%s: chýba %s%s" % (lang, b, s))

    def test_placeholders(self):
        var = re.compile(r"\{(\w+)\}")
        for k in self.sk:
            if k == "_meta" or VARS_EXEMPT.match(k):
                continue
            self.assertEqual(sorted(set(var.findall(self.sk[k]))), sorted(set(var.findall(self.en[k]))), "premenné sa líšia: " + k)

    def test_html_keys_only_known_tags(self):
        tag = re.compile(r"</?(\w+)")
        for lang, d in (("sk", self.sk), ("en", self.en)):
            for k, v in d.items():
                if k == "_meta":
                    continue
                tags = set(tag.findall(v))
                if not k.endswith("_html"):
                    self.assertEqual(tags, set(), "%s: HTML v preklade bez _html: %s" % (lang, k))
                else:
                    self.assertTrue(tags <= {"b", "i", "span"}, "%s: nepovolená značka v %s" % (lang, k))


def used_keys(text):
    """kľúče použité v kóde: každý reťazcový literál tvaru predpona.názov (pozri PREFIXES) a data-i18n atribúty"""
    found = {}   # kľúč -> True, ak je len predponou (v kóde za ním nasleduje `+`)
    for m in re.finditer(r'''(?<!data-path=)["'`]((?:%s)\.[a-z0-9_.]*)["'`](\s*\+)?''' % "|".join(PREFIXES), text):
        found[m.group(1)] = found.get(m.group(1), True) and bool(m.group(2))
    for m in re.finditer(r'data-i18n(?:-html)?="([^"]+)"', text):
        found[m.group(1)] = False
    for m in re.finditer(r'data-i18n-attr="([^"]+)"', text):
        for part in m.group(1).split(";"):
            if ":" in part:
                found[part.split(":", 1)[1].strip()] = False
    return found


class Usage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sk = load_lang("sk")[0]

    def check_file(self, name):
        text = read(name)
        missing = []
        for k, prefix_only in sorted(used_keys(text).items()):
            if k in self.sk:
                continue
            if any(k + s in self.sk for s in PLURAL):      # tn("kľúč", n)
                continue
            if (prefix_only or k.endswith(".")) and any(x.startswith(k) for x in self.sk):   # skladaný kľúč: "predpona" + premenná
                continue
            missing.append(k)
        self.assertEqual(missing, [], "%s používa kľúče, ktoré nie sú v sk.json: %s" % (name, missing))

    def test_ui_html(self):
        self.check_file("ui.html")

    def test_stats_ui_js(self):
        if os.path.isfile(os.path.join(ROOT, "stats_ui.js")):
            self.check_file("stats_ui.js")

    def test_ui_has_i18n_contract(self):
        h = read("ui.html")
        for name in ("function t(", "function tn(", "function fnum(", "function applyI18n(", "const i18nReady", "var LANG", "var LOCALE", "kurenie-lang"):
            self.assertIn(name, h, name)
        self.assertIn("i18nReady.then(() => gateEntry())", h)


def strip_js_comments(js):
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(^|[\s;{}()])//[^\n]*", r"\1", js)


class NoHardcodedSlovak(unittest.TestCase):
    """v ui.html nesmie ostať slovenský text mimo komentárov a mimo zdôvodnených výnimiek (ALLOW_*)"""

    @classmethod
    def setUpClass(cls):
        h = read("ui.html")
        i = h.index("<script>\nconst $ =")
        cls.head_body, cls.js = h[:i], h[i:]

    def test_html(self):
        h = re.sub(r"<style>.*?</style>", "", self.head_body, flags=re.S)
        h = re.sub(r"<!--.*?-->", "", h, flags=re.S)
        h = re.sub(r"<script>.*?</script>", "", h, flags=re.S)
        # prvky s data-i18n(-html): text vo vnútri je len počiatočný, za behu sa nahradí prekladom
        prev = None
        while prev != h:
            prev = h
            h = re.sub(r"<(\w+)(?:\s[^<>]*)?\sdata-i18n(?:-html)?=\"[^\"]*\"[^<>]*>.*?</\1>", "", h, flags=re.S)
        # značky s data-i18n-attr: preložené atribúty sa prepíšu za behu
        h = re.sub(r"<\w+(?:\s[^<>]*)?\sdata-i18n-attr=\"[^\"]*\"[^<>]*>", "", h)
        bad = []
        for lit in sorted(ALLOW_HTML, key=len, reverse=True):
            h = h.replace(lit, "")
        for ln in h.split("\n"):
            if SK_CHARS.search(ln):
                bad.append(ln.strip()[:160])
        self.assertEqual(bad, [], "v HTML ostal slovenský text")

    def test_js(self):
        js = strip_js_comments(self.js)
        bad = []
        for lit in sorted(ALLOW_LITERALS, key=len, reverse=True):
            for q in ('"', "'", "`"):
                js = js.replace(q + lit + q, "")
        for n, ln in enumerate(js.split("\n"), 1):
            if SK_CHARS.search(ln):
                bad.append("%d: %s" % (n, ln.strip()[:160]))
        self.assertEqual(bad, [], "v JS ostal slovenský text (alebo pridaj zdôvodnenú výnimku)")


class Route(unittest.TestCase):
    def setUp(self):
        self.p_cfg = mock.patch.object(app, "load_cfg", lambda: {})
        self.p_cfg.start()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close(); self.p_cfg.stop()

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        c.request("GET", path)
        r = c.getresponse(); raw = r.read(); hdr = dict(r.getheaders()); c.close()
        return r.status, hdr, raw

    def test_locale_ok(self):
        for lang in ("sk", "en"):
            s, h, raw = self.get("/locales/%s.json" % lang)
            self.assertEqual(s, 200)
            self.assertEqual(h.get("Content-Type"), "application/json; charset=utf-8")
            self.assertEqual(h.get("Cache-Control"), "no-cache")
            self.assertEqual(json.loads(raw.decode("utf-8"))["_meta"]["jazyk"], lang)

    def test_locale_404(self):
        for p in ("/locales/../x", "/locales/xx.json", "/locales/SK.json", "/locales/sk.json/x", "/locales/%2e%2e/app.py", "/locales/sk", "/locales/stats.sk.json", "/locales/../app.py"):
            self.assertEqual(self.get(p)[0], 404, p)

    def test_no_login_needed_and_manifest_lang(self):
        self.assertEqual(self.get("/locales/sk.json")[0], 200)
        self.assertEqual(app.MANIFEST["lang"], "sk")


if __name__ == "__main__":
    unittest.main()
