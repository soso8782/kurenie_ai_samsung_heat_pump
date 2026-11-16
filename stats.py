#!/usr/bin/env python3
"""Štatistiky tepelného čerpadla (SPEC_STATS.md, balík A): trvalá databáza stats.db, hodinová agregácia z tc.db,
cykly kompresora, poruchy, dotazové funkcie pre web, export CSV a vlákno `stats_loop`.

tc.db a agent.db sa otvárajú iba na čítanie (mode=ro); zapisuje sa výhradne do stats.db. Iba štandardná knižnica.
"""
import bisect, csv, datetime as _dt, io, json, os, re, sqlite3, time, traceback
from urllib.parse import quote

import profil
from common import ROOT, cfg_int, load_cfg
from i18n import tr

SCHEMA_VERSION = "1"
HOUR = 3600
PREV_S = 600            # ako ďaleko pred hodinou hľadáme predchádzajúcu vzorku (štarty, počítadlo kWh)
GAP_S = 600             # medzera vo vzorkách > 10 min = výpadok dát
MERGE_GAP_S = 120       # medzera <= 2 min medzi bežiacimi vzorkami spája cyklus
OPEN_FRESH_S = 600      # cyklus/porucha je „bežiaca“ len ak posledná vzorka nie je staršia ako 10 min
CHUNK_H = 168           # agregácia po týždňoch

# ---------------------------------------------------------------- entity a stĺpce
# mapy entít, stĺpce a model zariadenia sa plnia z profilu (profil.json) v configure() na konci modulu
TC = {}
INV = {}
CP_WATER = 4.186          # kJ/(kg·K); voda, ρ ≈ 1 kg/L
FLOW_MIN, FLOW_MAX = 1.0, 100.0   # platný prietok v L/min (senzor pri nedostupnosti hlási 6553,5)
COP_MIN_COV = 0.7         # aspoň 70 % bežiacich minút musí mať platný prietok a teploty
COP_MIN_KWH = 0.05        # pod touto spotrebou za hodinu sa COP nepočíta
K_LO, K_HI = 0.6, 1.5     # povolený rozsah kalibračného koeficientu
CAL_BASE_DAYS, CAL_DAYS = 30, 60
CAL_MIN_RUN_MIN, CAL_MIN_DAY_HOURS, CAL_MIN_DAYS, CAL_MIN_BASE_N = 50, 6, 3, 5
PURE_RUN_MIN, PURE_SHARE = 30, 0.9   # „čistá“ hodina pre COP kúrenia/TÚV: beh >= 30 min a >= 90 % v jednom režime
NIGHT_MIN_N = 12          # kalibrácia z nočných hodín (0–4 h): aspoň toľko hodín, rozpätie kwh >= NIGHT_MIN_SPAN
NIGHT_MIN_SPAN = 0.8
NIGHT_MIN_RUN, NIGHT_MIN_KWH = 20, 0.3   # nočná hodina: beh >= 20 min a spotreba čerpadla >= 0,3 kWh
FLOWCAL_DAYS = 30         # z koľkých dní minútových vzoriek sa kalibruje prietok okruhov
FLOWCAL_MIN_ROOM, FLOWCAL_MIN_TANK = 300, 60   # najmenší počet ustálených minút pre kúrenie / TÚV
FLOWCAL_SETTLE_MIN = 15   # kompresor musí bežať aj v tejto dobe pred minútou (min)
FLOWCAL_RANGE = {"room": (8.0, 60.0), "tank": (10.0, 90.0)}   # prijateľný rozsah prietoku [L/min]
FLOWCAL_ZDROJ = ""
VALVE = DEFROST = THERMOSTAT = ""
AGENT_TEMPS = {}
MODEL = None
ALERT_KINDS = ("chyba_tc", "esp", "zberac", "dohrev", "agent_chyba", "ha")

ROOM_KEYS = ()
HCOLS = ()
AVG_KEYS = ()
NEW_COLS = (("kwh_dom", "REAL"), ("kwh_cal", "REAL"), ("q_kwh", "REAL"), ("cop", "REAL"))   # pridané neskôr (ALTER TABLE)
_COL_RE = re.compile(r"^[a-z][a-z0-9_]{0,20}$")     # rovnaký tvar ako kľúče v profile (do SQL ide len takto overený názov)
_EXTRA_NAMES = {"chodba": "Chodba", "kotolna": "Kotolňa"}


def _m(key, label, unit, group, decimals, agg):
    return {"key": key, "label": label, "unit": unit, "group": group, "decimals": decimals, "agg": agg}


_M_HEAD = [
    ("vonku", "Vonku", "°C", "vonku", 1, "avg"),
    ("ciel", "Referenčná teplota", "°C", "izby", 1, "avg"),
]
_M_TAIL = [
    ("tw2", "Voda do domu (výstup)", "°C", "voda", 1, "avg"),
    ("tw1", "Spiatočka", "°C", "voda", 1, "avg"),
    ("wl", "Cieľová voda (krivka)", "°C", "voda", 1, "avg"),
    ("dt_beh", "ΔT pri behu", "K", "voda", 1, "avg"),
    ("tuv", "TÚV (bojler)", "°C", "tuv", 1, "avg"),
    ("tuv_ciel", "Cieľ TÚV", "°C", "tuv", 1, "avg"),
    ("tuv_min", "Ohrev TÚV (min)", "min", "tuv", 0, "sum"),
    ("beh_min", "Chod kompresora (min)", "min", "kompresor", 0, "sum"),
    ("kurenie_min", "Kúrenie (min)", "min", "kompresor", 0, "sum"),
    ("odmraz_min", "Odmrazovanie (min)", "min", "kompresor", 0, "sum"),
    ("starty", "Štarty", "", "kompresor", 0, "sum"),
    ("hz_avg", "Frekvencia Ø", "Hz", "kompresor", 0, "avg"),
    ("hz_max", "Frekvencia max", "Hz", "kompresor", 0, "max"),
    ("kwh", "Spotreba (odhad) kWh", "kWh", "energia", 2, "sum"),
    ("kwh_cal", "Spotreba (kalibrovaná) kWh", "kWh", "energia", 2, "sum"),
    ("kwh_dom", "Dom spolu, kWh", "kWh", "energia", 2, "sum"),
    ("prikon", "Príkon Ø (W)", "W", "energia", 0, "avg"),
    ("q_kwh", "Vyrobené teplo, kWh", "kWh", "ucinnost", 2, "sum"),
    ("cop", "COP", "", "ucinnost", 2, "ratio"),
    ("n", "Pokrytie (vzoriek za hodinu)", "", "kvalita", 0, "avg"),
]
METRICS = []
METRIC_KEYS = []
_PROFILE = None
_MDEC = {}


def _metrics(p, lang="sk"):
    """Zoznam metrík pre UI štatistík: izby a ďalšie sledované teploty podľa profilu (referenčná izba je „riadiaca“).
    Názvy v jazyku `lang` (sk = pôvodné texty); názvy izieb z profilu sa neprekladajú."""
    ref = profil.reference_room(p)
    names = profil.room_names(p)

    def lb(x):
        return x[1] if lang == "sk" else tr("srv.stats.m_" + x[0], lang)
    out = [_m(x[0], lb(x), *x[2:]) for x in _M_HEAD]
    for k in profil.room_keys(p):
        out.append(_m(k, names[k] + ((" (riadiaca)" if lang == "sk" else " " + tr("srv.stats.riadiaca", lang)) if k == ref else ""), "°C", "izby", 1, "avg"))
    for k in profil.extra_temp_keys(p):
        nm = _EXTRA_NAMES.get(k)
        if nm and lang != "sk":
            nm = tr("srv.stats.x_" + k, lang)
        out.append(_m(k, nm or k.replace("_", " ").capitalize(), "°C", "izby", 1, "avg"))
    return out + [_m(x[0], lb(x), *x[2:]) for x in _M_TAIL]


def metrics(lang="sk"):
    """Metriky v jazyku požiadavky (sk = METRICS)."""
    return METRICS if lang == "sk" or _PROFILE is None else _metrics(_PROFILE, lang)


# poruchy: predvyplnené kódy (len INSERT OR IGNORE, úpravy používateľa sa nikdy neprepíšu)
SEED_CODES = [
    ("E911", "Nízky prietok vody", "Prietokový spínač nehlási prietok, hoci obehové čerpadlo beží.",
     "Skontroluj, či nie sú priškrtené alebo zavreté ventily, sitko pred hydroboxom, tlak vody (1–2,5 bar) a odvzdušnenie."),
    ("DATA", "Výpadok dát", "Z tepelného čerpadla neprichádzali minútové vzorky (chýba aspoň 10 minút).",
     "Skontroluj ESP modul a zberač dát; porucha sa uzavrie sama po obnovení dát."),
    ("ESP", "ESP neodpovedá", "Modul ESP pri tepelnom čerpadle neodpovedá na požiadavky agenta.",
     "Skontroluj napájanie a Wi-Fi modulu ESP, prípadne ho reštartuj."),
    ("ZBERAC", "Zberač dát nefunguje", "Služba, ktorá ukladá údaje z tepelného čerpadla, nefunguje.",
     "Skontroluj službu zberača na serveri a jej záznam (log)."),
    ("DOHREV", "Dlho beží elektrický dohrev", "Elektrický dohrev beží neobvykle dlho.",
     "Skontroluj nastavenie dohrevu a vonkajšiu jednotku tepelného čerpadla."),
    ("AGENT_CHYBA", "Chyba agenta", "Agent kúrenia hlásil chybu pri svojej práci.", "Pozri záznam agenta a opakuj akciu."),
    ("HA", "Home Assistant neodpovedá", "Home Assistant neodpovedal na požiadavky agenta.",
     "Skontroluj, či Home Assistant beží a je dostupný zo servera."),
    ("CHYBA_TC", "Chyba tepelného čerpadla (upozornenie)", "Agent poslal upozornenie na chybový kód tepelného čerpadla.",
     "Pozri presný kód v texte poruchy a doplň ho do zoznamu kódov."),
]

_SEED_KEY = {c[0]: c[1].lower() for c in (("E911", "e911"), ("DATA", "data"), ("ESP", "esp"), ("ZBERAC", "zberac"), ("DOHREV", "dohrev"),
                                           ("AGENT_CHYBA", "agent_chyba"), ("HA", "ha"), ("CHYBA_TC", "chyba_tc"))}
_SEED_BY = {c[0]: c[1:] for c in SEED_CODES}


def _seed_tr(code, idx, val, lang):
    """Predvyplnený (neupravený) text kódu poruchy sa pre iný jazyk ako sk preloží; upravený používateľom ostáva."""
    if lang == "sk" or code not in _SEED_BY or _SEED_BY[code][idx] != val:
        return val
    return tr("srv.stats.seed_%s_%s" % (_SEED_KEY[code], ("title", "desc", "fix")[idx]), lang)


DDL = """
CREATE TABLE IF NOT EXISTS cycles(id INTEGER PRIMARY KEY, ts_start INTEGER, ts_stop INTEGER, minutes REAL, f_max REAL, f_avg REAL,
  mode TEXT, vonku REAL, tw2_max REAL, reason TEXT, UNIQUE(ts_start));
CREATE TABLE IF NOT EXISTS faults(id INTEGER PRIMARY KEY, ts_start INTEGER, ts_end INTEGER, code TEXT, source TEXT, title TEXT,
  text TEXT, auto_key TEXT UNIQUE, created INTEGER);
CREATE TABLE IF NOT EXISTS fault_codes(code TEXT PRIMARY KEY, title TEXT, description TEXT, fix TEXT, editable INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS cycles_ts ON cycles(ts_start);
CREATE INDEX IF NOT EXISTS faults_ts ON faults(ts_start);
"""


# ---------------------------------------------------------------- spojenia
def _db_path(cfg):
    return (cfg.get("STATS_DB") or "").strip() or os.path.join(ROOT, "stats.db")


def _timeout(cfg):
    try:
        return max(0.1, float(cfg.get("STATS_TIMEOUT") or 30))
    except (TypeError, ValueError):
        return 30.0


def hourly_ddl():
    """CREATE TABLE hourly so stĺpcami podľa profilu (pri predvolenom profile rovnaké stĺpce ako vo verzii 1)."""
    cols = ["ts INTEGER PRIMARY KEY", "n INTEGER", "src TEXT"]
    cols += ["%s %s" % (c, "INTEGER" if c == "starty" else "REAL") for c in HCOLS]
    cols += ["updated INTEGER"]
    return "CREATE TABLE IF NOT EXISTS hourly(%s);\n" % ", ".join(cols)


def init_schema(con):
    """Vytvorí tabuľky, indexy a predvyplní zoznam kódov porúch. Idempotentné, nič neprepisuje."""
    con.executescript(hourly_ddl() + DDL)
    for code, title, desc, fix in SEED_CODES:
        con.execute("INSERT OR IGNORE INTO fault_codes(code, title, description, fix, editable) VALUES(?,?,?,?,1)",
                    (code, title, desc, fix))
    con.execute("INSERT OR IGNORE INTO meta(k, v) VALUES('schema', ?)", (SCHEMA_VERSION,))
    con.commit()


def migrate(con):
    """Idempotentná migrácia schémy: doplní chýbajúce stĺpce hourly (kwh_dom, kwh_cal, q_kwh, cop a stĺpce izieb z profilu).
    Stĺpce sa nikdy nemažú."""
    try:
        have = {r[1] for r in con.execute("PRAGMA table_info(hourly)")}
    except sqlite3.Error:
        return
    if not have:
        return
    for col in HCOLS:
        typ = "INTEGER" if col == "starty" else "REAL"
        if col not in have and _COL_RE.match(col):
            try:
                con.execute("ALTER TABLE hourly ADD COLUMN %s %s" % (col, typ))
            except sqlite3.OperationalError:       # súbežný proces ho už pridal
                pass
    con.commit()


def connect(cfg):
    """Spojenie na stats.db (zápis povolený), schéma sa vytvorí pri prvom použití."""
    con = sqlite3.connect(_db_path(cfg), timeout=_timeout(cfg))
    try:
        try:
            con.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        con.execute("PRAGMA synchronous=NORMAL")
        try:
            ok = con.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
        except sqlite3.OperationalError:
            ok = None
        if not ok:
            init_schema(con)
        migrate(con)
    except Exception:
        con.close()
        raise
    return con


def _ro(path, timeout=30):
    return sqlite3.connect("file:%s?mode=ro" % quote(path), uri=True, timeout=timeout)


def _tc(cfg):
    return _ro(cfg["TC_DB"], _timeout(cfg))


def _agent_ro(cfg):
    p = cfg.get("AGENT_DB")
    if not p or not os.path.exists(p):
        return None
    try:
        return _ro(p, _timeout(cfg))
    except sqlite3.Error:
        return None


def _meta_set(con, k, v):
    con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?,?)", (k, str(v)))


# ---------------------------------------------------------------- čas a dátumy
_DRE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _pdate(s, lang="sk"):
    if isinstance(s, _dt.datetime):
        return s.date()
    if isinstance(s, _dt.date):
        return s
    bad = ValueError(tr("srv.stats.datum_neplatny", lang, s=s))
    if not isinstance(s, str) or not _DRE.match(s.strip()):
        raise bad
    try:
        d = _dt.date.fromisoformat(s.strip())
    except ValueError:
        raise bad
    if not 2000 <= d.year <= 2100:
        raise ValueError(tr("srv.stats.datum_rozsah", lang))
    return d


def _midnight(d):
    return int(time.mktime((d.year, d.month, d.day, 0, 0, 0, 0, 0, -1)))


def _bounds(d):
    """(a, b) lokálna polnoc dňa a nasledujúceho dňa; deň má 23–25 hodín."""
    return _midnight(d), _midnight(d + _dt.timedelta(days=1))


def _dstr(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _today():
    return _dt.date.fromtimestamp(time.time())


def _shift_years(d, n):
    try:
        return d.replace(year=d.year - n)
    except ValueError:                       # 29. 2. -> 28. 2.
        return d.replace(year=d.year - n, day=28)


def _slk(d, lang="sk"):
    return tr("srv.stats.datum", lang, d=d.day, m=d.month, y=d.year)


def _hbase(cfg):
    return cfg_int(cfg, "STATS_HDD_BASE", 18, 10, 25)


# ---------------------------------------------------------------- stav z udalostí (ventil, odmrazovanie)
class _Track:
    """Stav entity v čase z tabuľky events: at(t) = posledná hodnota s ts <= t."""

    def __init__(self, initial, evs):
        self.t = [-1]
        self.v = [initial]
        for ts, val in evs:
            self.t.append(ts)
            self.v.append(val)

    def at(self, t):
        return self.v[bisect.bisect_right(self.t, t) - 1]

    def intervals(self, a, b, last_ts):
        """Intervaly [od, do) so stavom True v rozsahu [a, b); otvorený interval sa končí najneskôr v last_ts+60."""
        out = []
        start = None
        for t, v in zip(self.t, self.v):
            if v and start is None:
                start = max(t, a)
            elif not v and start is not None:
                out.append((start, min(t, b)))
                start = None
        if start is not None:
            end = min(b, max(start, last_ts + 60))
            out.append((start, end))
        return [(s, e) for s, e in out if e > s and e > a and s < b]


def _valve_val(new):
    s = str(new or "")
    return "tank" if "Tank" in s else ("room" if "Room" in s else None)


def _defr_val(new):
    s = str(new or "").strip().upper()
    return True if s == "ON" else (False if s == "OFF" else None)


def _tracks(tc, a, b):
    out = []
    for ent, fn in ((VALVE, _valve_val), (DEFROST, _defr_val)):
        r = tc.execute("SELECT new FROM events WHERE entity=? AND ts<? ORDER BY ts DESC LIMIT 1", (ent, a)).fetchone()
        init = fn(r[0]) if r else None
        evs = [(ts, fn(nw)) for ts, nw in tc.execute(
            "SELECT ts, new FROM events WHERE entity=? AND ts>=? AND ts<? ORDER BY ts", (ent, a, b))]
        out.append(_Track(init, evs))
    return out


# ---------------------------------------------------------------- hodinová agregácia
def _upsert_sql():
    cols = ",".join(HCOLS)
    ph = ",".join("?" * (len(HCOLS) + 4))
    # kwh_dom (elektromer) sa len dopĺňa: existujúca hodnota sa nikdy neprepíše
    sets = ",".join(("%s=COALESCE(hourly.%s, excluded.%s)" if c == "kwh_dom" else "%s=COALESCE(excluded.%s, hourly.%s)") % (c, c, c)
                    for c in HCOLS)
    return ("INSERT INTO hourly(ts, n, src, %s, updated) VALUES(%s) ON CONFLICT(ts) DO UPDATE SET "
            "n=COALESCE(excluded.n, hourly.n), "
            "src=CASE WHEN excluded.src='tc' AND hourly.src IN ('ha','mix') THEN 'mix' "
            "ELSE COALESCE(excluded.src, hourly.src) END, %s, updated=excluded.updated" % (cols, ph, sets))


_UPSERT = ""


def _upsert_rows(con, rows):
    now = int(time.time())
    con.executemany(_UPSERT, [(r["ts"], r.get("n"), r.get("src") or "tc", *[r.get(c) for c in HCOLS],
                               r.get("updated") or now) for r in rows])


def upsert_hourly(cfg, rows):
    """UPSERT hodinových riadkov (slovníky s kľúčom ts a stĺpcami z HCOLS). Hodnota NULL nikdy neprepíše existujúcu hodnotu."""
    con = connect(cfg)
    try:
        _upsert_rows(con, rows)
        con.commit()
        return len(rows)
    finally:
        con.close()


def _agent_hourly(cfg, a, b):
    """Priemery chodby a kotolne z agent.db (temp_samples) po hodinách: {hodina: {stĺpec: hodnota}}."""
    out = {}
    if not AGENT_TEMPS:
        return out
    ac = _agent_ro(cfg)
    if ac is None:
        return out
    try:
        inv = {v: k for k, v in AGENT_TEMPS.items()}
        rows = ac.execute("SELECT entity_id, ts - ts %% 3600, avg(value) FROM temp_samples WHERE ts>=? AND ts<? "
                          "AND entity_id IN (%s) GROUP BY 1,2" % ",".join("?" * len(AGENT_TEMPS)),
                          (a, b, *AGENT_TEMPS.values())).fetchall()
        for ent, h, v in rows:
            if v is not None:
                out.setdefault(h, {})[inv[ent]] = round(v, 2)
    except sqlite3.Error:
        pass
    finally:
        ac.close()
    return out


def flow_ok(v):
    """Platný prietok v L/min (6553,5 = hodnota „nedostupné“ zo senzora)."""
    return v is not None and FLOW_MIN <= v <= FLOW_MAX


def _cfg_lpm(cfg, key):
    try:
        v = float((cfg or {}).get(key) or 0)
    except (TypeError, ValueError):
        return None
    return v if flow_ok(v) else None


def cfg_flows(cfg):
    """Zmerané prietoky v L/min z config.env: STATS_FLOW_LPM (kúrenie) a STATS_FLOW_TUV_LPM (TÚV); nenastavené/neplatné = None.
    Ak je nastavené len STATS_FLOW_LPM, platí iba pre kúrenie."""
    return {"room": _cfg_lpm(cfg, "STATS_FLOW_LPM"), "tank": _cfg_lpm(cfg, "STATS_FLOW_TUV_LPM")}


def cfg_flow(cfg):
    """Pevný prietok pre kúrenie z config.env (STATS_FLOW_LPM); None = nepoužiť. Obal nad cfg_flows."""
    return cfg_flows(cfg)["room"]


def flow_for(st, sensor, cfg_flows, cal_flows):
    """Prietok minúty (L/min) a jeho zdroj: (hodnota, "senzor" | "zmerany" | "tabulky") alebo (None, None).
    Poradie: platný senzor, hodnoty z config.env, kalibrácia z tabuliek výrobcu. st = poloha ventilu "room"/"tank"/None
    (neznáma poloha = kúrenie)."""
    if flow_ok(sensor):
        return sensor, "senzor"
    ck = "tank" if st == "tank" else "room"
    for src, zdroj in ((cfg_flows, "zmerany"), (cal_flows, "tabulky")):
        v = (src or {}).get(ck)
        if flow_ok(v):
            return v, zdroj
    return None, None


def pth_kw(flow, tw2, tw1):
    """Tepelný výkon v kW: prietok [L/min] / 60 × 4,186 × (Tw2 − Tw1). Záporné ΔT (odmrazovanie) dáva zápornú hodnotu:
    teplo sa vtedy z domu odoberá a odpočíta sa (nezápornosť sa vynucuje až na úrovni hodiny)."""
    return (tw2 - tw1) * flow / 60.0 * CP_WATER


# ---------------------------------------------------------------- tabuľka výrobcu a prietok podľa okruhu
TAB_TA = ()          # tabuľka výrobcu sa berie z modelu zariadenia (modely/<id>.json cez profil.model()); bez modelu je prázdna
TAB_LW = ()
TAB_PEAK = {}        # {ta: ((HC kW, PI kW) | None, …)}, Maximum Heating Capacity (Peak Value)


def _tab_cell(ta, j):
    """Bunka (HC, PI) riadku ta pre stĺpec j; None = posledná platná bunka v riadku (zľava)."""
    row = TAB_PEAK[ta]
    while j >= 0 and row[j] is None:
        j -= 1
    return row[max(j, 0)]


def _tab_pos(axis, x):
    """Index dolného bodu a podiel v intervale pre hodnotu x orezanú na rozsah osi."""
    x = min(max(float(x), axis[0]), axis[-1])
    i = max(0, min(bisect.bisect_right(axis, x) - 1, len(axis) - 2))
    return i, (x - axis[i]) / float(axis[i + 1] - axis[i])


def cop_tab(tamb, lwt):
    """COP = HC/PI z tabuľky výrobcu (Peak = bez odmrazovania): bilineárna interpolácia COP v bunkách tabuľky podľa vonkajšej
    teploty a teploty výstupnej vody (zodpovedá kontrolným hodnotám zo SPEC_cop); vstupy sa orezávajú na rozsah tabuľky.
    Bez modelu zariadenia (žiadna tabuľka) vráti None."""
    if not TAB_PEAK:
        return None
    i, fi = _tab_pos(TAB_TA, tamb)
    j, fj = _tab_pos(TAB_LW, lwt)
    cop = 0.0
    for di, wi in ((0, 1.0 - fi), (1, fi)):
        for dj, wj in ((0, 1.0 - fj), (1, fj)):
            hc, pi = _tab_cell(TAB_TA[i + di], j + dj)
            cop += wi * wj * hc / pi
    return cop


def _pct(xs, p):
    """Percentil p (0–1) zoradeného zoznamu s lineárnou interpoláciou."""
    n = len(xs)
    if not n:
        return None
    pos = (n - 1) * p
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def flow_calc(samples, valve, defr_int, k):
    """Čistý výpočet prietoku okruhov z tabuľky výrobcu v ustálených minútach (L/min). samples = {kľúč: {minúta_ts: hodnota}} pre
    freq, tw2, tw1, prikon, vonku; valve.at(ts) -> "room"/"tank"/None; defr_int = [(začiatok, koniec)] odmrazovaní; k = kalibračný
    koeficient príkonu (None = 1,0). Ustálená minúta: kompresor beží aj v 15 predošlých minútach, žiadne odmrazovanie v okolí
    (600 s pred, 900 s po), ventil v t aj t − 600 rovnaký, ΔT >= 1,5 a príkon >= 300 W. Vracia {"kurenie", "tuv", "n_kurenie",
    "n_tuv", "p25_*", "p75_*"}; medián iba pri dosť minútach a v rozumnom rozsahu, inak None."""
    k = k if k else 1.0
    fq, t2, t1 = samples.get("freq", {}), samples.get("tw2", {}), samples.get("tw1", {})
    pk, vk = samples.get("prikon", {}), samples.get("vonku", {})
    ivs = sorted((s - 600, e + 900) for s, e in (defr_int or ()))
    starts = [s for s, _ in ivs]
    ends = []                                          # prefixové maximum koncov (intervaly sa môžu prekrývať)
    for _, e in ivs:
        ends.append(max(e, ends[-1]) if ends else e)
    vals = {"room": [], "tank": []}
    for t in sorted(fq):
        if fq[t] <= 0 or t not in t2 or t not in t1 or t not in pk or t not in vk:
            continue
        if any(fq.get(t - 60 * i, 0) <= 0 for i in range(1, FLOWCAL_SETTLE_MIN + 1)):
            continue
        n = bisect.bisect_right(starts, t)
        if n and ends[n - 1] >= t:
            continue
        v = valve.at(t)
        if v not in vals or valve.at(t - 600) != v:
            continue
        dt = t2[t] - t1[t]
        if dt < 1.5 or pk[t] < 300:
            continue
        ct = cop_tab(vk[t], t2[t])
        if ct is None:                                 # bez tabuľky výrobcu sa prietok nekalibruje
            continue
        vals[v].append(ct * pk[t] * k / 1000.0 / (CP_WATER * dt / 60.0))
    out = {}
    for key, name, need in (("room", "kurenie", FLOWCAL_MIN_ROOM), ("tank", "tuv", FLOWCAL_MIN_TANK)):
        xs = sorted(vals[key])
        lo, hi = FLOWCAL_RANGE[key]
        med = _median(xs)
        out[name] = round(med, 1) if len(xs) >= need and lo <= med <= hi else None
        out["n_" + name] = len(xs)
        out["p25_" + name] = round(_pct(xs, 0.25), 1) if xs else None
        out["p75_" + name] = round(_pct(xs, 0.75), 1) if xs else None
    return out


def get_flows(con):
    """Automaticky kalibrovaný prietok okruhov z meta `prietok`: {"room": lpm|None, "tank": lpm|None}."""
    d = get_prietok(con) or {}
    out = {}
    for key, name in (("room", "kurenie"), ("tank", "tuv")):
        v = d.get(name)
        out[key] = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and flow_ok(v) else None
    return out


def get_prietok(con):
    """Uložený výsledok kalibrácie prietoku z meta (slovník alebo None)."""
    try:
        r = con.execute("SELECT v FROM meta WHERE k='prietok'").fetchone()
        d = json.loads(r[0]) if r and r[0] else None
    except (sqlite3.Error, ValueError):
        return None
    return d if isinstance(d, dict) else None


def flow_calibrate(cfg, now=None):
    """Prietok okruhov z tabuliek výrobcu za posledných FLOWCAL_DAYS dní minútových vzoriek tc.db; výsledok uloží do meta `prietok`.
    Bez modelu (tabuľky) sa kalibrácia preskočí: uloží sa prázdny výsledok (prietok zo senzora / z konfigurácie).
    Nikdy nevyhodí výnimku (pri chybe vráti {"error": ...} a meta nemení)."""
    try:
        now = int(now or time.time())
        if not TAB_PEAK:
            res = {"kurenie": None, "tuv": None, "n_kurenie": 0, "n_tuv": 0, "p25_kurenie": None, "p75_kurenie": None,
                   "p25_tuv": None, "p75_tuv": None, "ts": now, "datum": time.strftime("%Y-%m-%d %H:%M", time.localtime(now)),
                   "zdroj": FLOWCAL_ZDROJ, "dovod": "model zariadenia bez tabuľky výrobcu"}
            con = connect(cfg)
            try:
                _meta_set(con, "prietok", json.dumps(res, ensure_ascii=False))
                con.commit()
            finally:
                con.close()
            return res
        a = now - FLOWCAL_DAYS * 86400
        tc = _tc(cfg)
        try:
            keys = ("freq", "tw2", "tw1", "prikon", "vonku")
            samples = {x: {} for x in keys}
            for ts, ent, v in tc.execute("SELECT ts, entity, value FROM samples WHERE ts>=? AND ts<=? AND entity IN (%s) ORDER BY ts"
                                         % ",".join("?" * len(keys)), (a, now, *[TC.get(x, "") for x in keys])):
                if v is not None and ent in INV:
                    samples[INV[ent]][ts - ts % 60] = v
            valve, defr = _tracks(tc, a, now + 1)
            last = tc.execute("SELECT max(ts) FROM samples").fetchone()[0] or now
            defr_int = defr.intervals(a, now + 1, last)
        finally:
            tc.close()
        con = connect(cfg)
        try:
            k = get_k(con)
            res = flow_calc(samples, valve, defr_int, k)
            res["ts"] = now
            res["datum"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
            res["zdroj"] = FLOWCAL_ZDROJ
            _meta_set(con, "prietok", json.dumps(res, ensure_ascii=False))
            con.commit()
        finally:
            con.close()
        return res
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}


def _den(r):
    """Spotreba, s ktorou sa počíta COP a súhrny: kalibrovaná, ak je, inak odhad."""
    v = r.get("kwh_cal")
    return v if v is not None else r.get("kwh")


def hour_cop(q, kwh, kwh_cal):
    den = kwh_cal if kwh_cal is not None else kwh
    if q is None or den is None or den < COP_MIN_KWH:
        return None
    return round(q / den, 3)


def _agg_chunk(cfg, tc, a, b, last_ts, kcal=None, flows=None):
    """Hodinové riadky pre [a, b) z tc.db (zoznam slovníkov, zatiaľ nezapísaný). k = kalibračný koeficient spotreby (alebo None),
    flows = kalibrované prietoky okruhov z get_flows (alebo None)."""
    names = list(TC.values())
    q = ",".join("?" * len(names))
    by = {}
    for ts, ent, v in tc.execute("SELECT ts, entity, value FROM samples WHERE ts>=? AND ts<? AND entity IN (%s) ORDER BY ts" % q,
                                 (a - PREV_S, b, *names)):
        k = INV.get(ent)
        if k is not None and v is not None:
            by.setdefault(k, []).append((ts, v))
    res = {}
    cnt = {}

    def row(h):
        return res.setdefault(h, {"ts": h, "src": "tc"})

    for k in AVG_KEYS:
        acc = {}
        for ts, v in by.get(k, ()):
            if ts < a:
                continue
            h = ts - ts % HOUR
            x = acc.get(h)
            if x is None:
                acc[h] = [v, 1, v, v]
            else:
                x[0] += v
                x[1] += 1
                x[2] = min(x[2], v)
                x[3] = max(x[3], v)
        for h, x in acc.items():
            r = row(h)
            r[k] = round(x[0] / x[1], 2)
            cnt[h] = max(cnt.get(h, 0), x[1])
            if k == "vonku":
                r["vonku_min"] = round(x[2], 2)
                r["vonku_max"] = round(x[3], 2)
                r["_nv"] = x[1]
    # spotreba: súčet kladných prírastkov denného počítadla (rovnaká logika ako common.hour_stats)
    pv = None
    kacc = {}
    for ts, v in by.get("kwh", ()):
        if ts >= a:
            h = ts - ts % HOUR
            kacc.setdefault(h, 0.0)
            cnt[h] = max(cnt.get(h, 0), 1)
            if pv is not None and 0 < v - pv < 2:
                kacc[h] += v - pv
        pv = v
    for h, x in kacc.items():
        row(h)["kwh"] = round(x, 3)
    # kompresor
    fr = by.get("freq", [])
    if fr:
        valve, defr = _tracks(tc, a, b)
        dts = dict(by.get("dt", ()))
        t2d, t1d, fld = dict(by.get("tw2", ())), dict(by.get("tw1", ())), dict(by.get("flow", ()))
        flow_fix = cfg_flows(cfg)
        accf = {}
        prev = None
        for ts, v in fr:
            on = v > 0
            if ts >= a:
                h = ts - ts % HOUR
                x = accf.setdefault(h, [0, 0, 0.0, 0.0, 0, 0, 0, 0.0, 0, 0, 0.0])
                x[0] += 1
                if on:
                    x[1] += 1
                    x[2] += v
                    x[3] = max(x[3], v)
                    st = valve.at(ts)
                    if st == "tank":
                        x[5] += 1
                    elif st == "room":
                        x[6] += 1
                    if v >= 20 and ts in dts:
                        x[7] += dts[ts]
                        x[8] += 1
                    fl, _zdroj = flow_for(st, fld.get(ts), flow_fix, flows)
                    if fl is not None and ts in t2d and ts in t1d:      # teplo: len minúty s platným prietokom a teplotami
                        x[9] += 1
                        x[10] += pth_kw(fl, t2d[ts], t1d[ts]) / 60.0
                if prev is not None and not prev[1] and on and ts - prev[0] <= PREV_S:
                    x[4] += 1
            prev = (ts, on)
        for h, x in accf.items():
            r = row(h)
            cnt[h] = max(cnt.get(h, 0), x[0])
            r["beh_min"] = round(60.0 * x[1] / x[0], 2)
            r["tuv_min"] = round(60.0 * x[5] / x[0], 2)
            r["kurenie_min"] = round(60.0 * x[6] / x[0], 2)
            r["starty"] = x[4]
            r["hz_avg"] = round(x[2] / x[1], 1) if x[1] else None
            r["hz_max"] = round(x[3], 1) if x[1] else None
            r["dt_beh"] = round(x[7] / x[8], 2) if x[8] else None
            r["odmraz_min"] = 0.0
            if x[1] and x[9] >= COP_MIN_COV * x[1]:      # dosť pokrytia: vyrobené teplo dopočítané na všetky bežiace minúty
                r["q_kwh"] = max(0.0, round(x[10] * x[1] / x[9], 3))     # záporný súčet (odmrazovanie) = 0 až za hodinu
        for s, e in defr.intervals(a, b, last_ts):
            h = s - s % HOUR
            while h < e:
                if h in accf:
                    ov = min(e, h + HOUR) - max(s, h)
                    if ov > 0:
                        res[h]["odmraz_min"] = round(res[h]["odmraz_min"] + ov / 60.0, 2)
                h += HOUR
    ag = _agent_hourly(cfg, a, b)
    out = []
    for h in sorted(res):
        if not (a <= h < b):
            continue
        r = res[h]
        for k, v in ag.get(h, {}).items():
            r[k] = v
        r["n"] = r.pop("_nv", None) or cnt.get(h) or 0
        if kcal and r.get("kwh") is not None:
            r["kwh_cal"] = round(r["kwh"] * kcal, 3)
        if r.get("q_kwh") is not None:
            r["cop"] = hour_cop(r["q_kwh"], r.get("kwh"), r.get("kwh_cal"))
        out.append(r)
    return out


def aggregate_range(cfg, a, b):
    """Prepočíta hodinové riadky pre interval [a, b) (unix sekundy, zarovná sa na celé hodiny). Vracia počet zapísaných hodín."""
    a = int(a) - int(a) % HOUR
    b = int(b) + (-int(b)) % HOUR
    if b <= a:
        return 0
    tc = _tc(cfg)
    try:
        r = tc.execute("SELECT max(ts) FROM samples").fetchone()
        last_ts = r[0] if r and r[0] is not None else 0
        con = connect(cfg)
        try:
            k = get_k(con)
            flows = get_flows(con)
            total = 0
            h = a
            while h < b:
                e = min(b, h + CHUNK_H * HOUR)
                rows = _agg_chunk(cfg, tc, h, e, last_ts, k, flows)
                if rows:
                    _upsert_rows(con, rows)
                    con.commit()
                    total += len(rows)
                h = e
            return total
        finally:
            con.close()
    finally:
        tc.close()


# ---------------------------------------------------------------- cykly kompresora
def _series(tc, ent, a):
    rows = tc.execute("SELECT ts, value FROM samples WHERE entity=? AND ts>=? AND value IS NOT NULL ORDER BY ts", (ent, a)).fetchall()
    return [r[0] for r in rows], [r[1] for r in rows]


def _extend_back(tc, ent, a):
    """Posunie začiatok okna tak, aby nezačínalo uprostred cyklu (hľadá nulovú vzorku, pred ktorou nebežalo)."""
    for _ in range(60):
        r = tc.execute("SELECT ts FROM samples WHERE entity=? AND ts<? AND value<=0 ORDER BY ts DESC LIMIT 1", (ent, a)).fetchone()
        if not r:
            return 0
        z = r[0]
        if not tc.execute("SELECT 1 FROM samples WHERE entity=? AND ts>=? AND ts<? AND value>0 LIMIT 1",
                          (ent, z - MERGE_GAP_S, z)).fetchone():
            return z
        a = z
    return a


def compute_cycles(cfg, a=0, now=None):
    """Zoznam cyklov (slovníky) z tc.db od času a (okno sa samo rozšíri na začiatok prvého cyklu)."""
    now = int(now or time.time())
    tc = _tc(cfg)
    try:
        A = _extend_back(tc, TC.get("freq", ""), int(a))
        fts, fv = _series(tc, TC.get("freq", ""), A)
        if not fts:
            return []
        vts, vv = _series(tc, TC.get("vonku", ""), A - PREV_S)
        wts, wv = _series(tc, TC.get("tw2", ""), A)
        valve, defr = _tracks(tc, A, 2 ** 40)
        stops = []      # (ts, druh) druh: 'term' = termostat OFF
        for ts, ent, new, text in tc.execute(
                "SELECT ts, entity, new, text FROM events WHERE ts>=? AND (entity=? OR (kind='kompresor' AND text LIKE 'STOP%')) ORDER BY ts",
                (A - PREV_S, THERMOSTAT)):
            if ent == THERMOSTAT:
                if str(new or "").strip().upper() == "OFF":
                    stops.append((ts, "term"))
            elif "termostat OFF" in (text or ""):
                stops.append((ts, "term"))
        stop_ts = [s[0] for s in stops]
        segs = []
        cur = None
        for i, (ts, v) in enumerate(zip(fts, fv)):
            if v > 0:
                if cur is not None and ts - cur["last"] <= MERGE_GAP_S:
                    cur["last"] = ts
                    cur["idx"].append(i)
                else:
                    cur = {"start": ts, "last": ts, "idx": [i]}
                    segs.append(cur)
        out = []
        last_sample = fts[-1]
        for s in segs:
            freqs = [fv[i] for i in s["idx"]]
            nxt = None
            li = s["idx"][-1]
            if li + 1 < len(fts):
                nxt = fts[li + 1]
            is_open = nxt is None and now - s["last"] <= OPEN_FRESH_S
            stop = None if is_open else s["last"] + (min(60, nxt - s["last"]) if nxt else 60)
            end_for_min = stop if stop is not None else s["last"] + 60
            minutes = round((end_for_min - s["start"]) / 60.0, 2)
            j = bisect.bisect_right(vts, s["start"]) - 1
            vonku = round(vv[j], 2) if j >= 0 and s["start"] - vts[j] <= PREV_S else None
            lo = bisect.bisect_left(wts, s["start"])
            hi = bisect.bisect_right(wts, s["last"])
            tw2max = round(max(wv[lo:hi]), 2) if hi > lo else None
            n_tank = n_room = n_def = 0
            for i in s["idx"]:
                t = fts[i]
                if defr.at(t):
                    n_def += 1
                st = valve.at(t)
                if st == "tank":
                    n_tank += 1
                elif st == "room":
                    n_room += 1
            tot = len(s["idx"])
            thr = min(3, tot)   #druh sa počíta, ak trval aspoň 3 minúty (šum jednej vzorky sa ignoruje)
            if n_def == tot:
                mode = "odmrazovanie"
            else:
                tu, ku = n_tank >= thr, n_room >= thr
                mode = "zmiesane" if (tu and ku) else ("tuv" if tu else ("kurenie" if ku else "zmiesane"))
            reason = None
            if stop is not None:
                T = stop
                d_end = bool(defr.at(s["last"])) or any(
                    t is True and T - 120 <= tt <= T + 60 for tt, t in zip(defr.t, defr.v))
                if d_end:
                    reason = "odmrazovanie"
                elif valve.at(s["last"]) == "tank":
                    reason = "TÚV"
                else:
                    k = bisect.bisect_left(stop_ts, T - 300)
                    reason = "dosiahnutý cieľ" if k < len(stop_ts) and stop_ts[k] <= T + 300 else "iné"
            out.append({"ts_start": s["start"], "ts_stop": stop, "minutes": minutes, "f_max": max(freqs),
                        "f_avg": round(sum(freqs) / len(freqs), 1), "mode": mode, "vonku": vonku,
                        "tw2_max": tw2max, "reason": reason})
        return out
    finally:
        tc.close()


def update_cycles(cfg, a=0, now=None):
    """Prepočíta cykly od času a a zapíše ich (UPSERT podľa ts_start). Vracia počet cyklov."""
    cyc = compute_cycles(cfg, a, now)
    if not cyc:
        return 0
    con = connect(cfg)
    try:
        con.executemany(
            "INSERT INTO cycles(ts_start, ts_stop, minutes, f_max, f_avg, mode, vonku, tw2_max, reason) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(ts_start) DO UPDATE SET ts_stop=excluded.ts_stop, minutes=excluded.minutes, "
            "f_max=COALESCE(excluded.f_max, cycles.f_max), f_avg=COALESCE(excluded.f_avg, cycles.f_avg), "
            "mode=COALESCE(excluded.mode, cycles.mode), vonku=COALESCE(excluded.vonku, cycles.vonku), "
            "tw2_max=COALESCE(excluded.tw2_max, cycles.tw2_max), reason=excluded.reason",
            [(c["ts_start"], c["ts_stop"], c["minutes"], c["f_max"], c["f_avg"], c["mode"], c["vonku"], c["tw2_max"], c["reason"])
             for c in cyc])
        con.commit()
        return len(cyc)
    finally:
        con.close()


# ---------------------------------------------------------------- poruchy
def _fault_upsert(con, f):
    con.execute(
        "INSERT INTO faults(ts_start, ts_end, code, source, title, text, auto_key, created) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(auto_key) DO UPDATE SET ts_end=COALESCE(excluded.ts_end, faults.ts_end)",
        (f["ts_start"], f.get("ts_end"), f["code"], f["source"], f.get("title") or "", f.get("text") or "", f["auto_key"],
         int(time.time())))


def detect_faults(cfg, a=0, now=None, ongoing=False):
    """Automatická detekcia porúch od času a (tc_error, gap, alert); idempotentné cez auto_key. Vracia počet spracovaných porúch.
    ongoing=True: aj prebiehajúci výpadok dát (posledná vzorka staršia ako 10 min) sa zapíše ako otvorená porucha."""
    now = int(now or time.time())
    a = int(a)
    con = connect(cfg)
    try:
        # okno sa rozšíri o začiatky doteraz otvorených porúch, aby sa dali zatvoriť
        r = con.execute("SELECT min(ts_start) FROM faults WHERE ts_end IS NULL AND source IN ('tc_error','gap')").fetchone()
        a_open = r[0] if r and r[0] is not None else None
        cnt = 0
        tc = _tc(cfg)
        try:
            # --- kód chyby TČ
            ent = TC.get("chyba", "")
            A = a if a_open is None else min(a, a_open)
            z = tc.execute("SELECT ts FROM samples WHERE entity=? AND ts<? AND value=0 ORDER BY ts DESC LIMIT 1", (ent, A)).fetchone()
            Ae = z[0] if z else 0
            rows = tc.execute("SELECT ts, value FROM samples WHERE entity=? AND ts>=? AND value IS NOT NULL ORDER BY ts", (ent, Ae)).fetchall()
            ivs = []
            curi = None
            for ts, v in rows:
                code = int(v)
                if code != 0:
                    if curi is not None and curi[0] == code:
                        continue
                    if curi is not None:
                        ivs.append((curi[0], curi[1], ts))
                    curi = (code, ts)
                elif curi is not None:
                    ivs.append((curi[0], curi[1], ts))
                    curi = None
            if curi is not None:
                last = rows[-1][0]
                ivs.append((curi[0], curi[1], None if now - last <= OPEN_FRESH_S else last + 60))
            for code, s, e in ivs:
                if e is not None and e < A:
                    continue
                _fault_upsert(con, {"ts_start": s, "ts_end": e, "code": "E%d" % code, "source": "tc_error",
                                    "text": "Chybový kód tepelného čerpadla E%d." % code,
                                    "auto_key": "tc_error:%d:%d" % (code, s)})
                cnt += 1
            # --- výpadky dát (medzera vo vzorkách vonkajšej teploty > 10 min)
            ent = TC.get("vonku", "")
            prev = tc.execute("SELECT ts FROM samples WHERE entity=? AND ts<? ORDER BY ts DESC LIMIT 1", (ent, A)).fetchone()
            prev = prev[0] if prev else None
            last = prev
            for (ts,) in tc.execute("SELECT ts FROM samples WHERE entity=? AND ts>=? AND value IS NOT NULL ORDER BY ts", (ent, A)):
                if last is not None and ts - last > GAP_S:
                    _fault_upsert(con, _gap(last + 60, ts))
                    cnt += 1
                last = ts
            if ongoing and last is not None and now - last > GAP_S:
                _fault_upsert(con, _gap(last + 60, None, now))
                cnt += 1
        finally:
            tc.close()
        # --- upozornenia agenta
        ac = _agent_ro(cfg)
        if ac is not None:
            try:
                for rid, ts, kind, title, msg in ac.execute(
                        "SELECT rowid, ts, kind, title, message FROM notify_log WHERE ts>=? AND kind IN (%s) ORDER BY ts"
                        % ",".join("?" * len(ALERT_KINDS)), (a, *ALERT_KINDS)):
                    con.execute("INSERT OR IGNORE INTO faults(ts_start, ts_end, code, source, title, text, auto_key, created) "
                                "VALUES(?,?,?,?,?,?,?,?)", (ts, ts, str(kind).upper(), "alert", title or "", msg or "",
                                                            "alert:%d" % rid, int(time.time())))
                    cnt += 1
            except sqlite3.Error:
                pass
            finally:
                ac.close()
        con.commit()
        return cnt
    finally:
        con.close()


def _gap(s, e, now=None):
    mins = ((e if e is not None else now) - s) / 60.0
    txt = ("Výpadok dát trvá už %d min." if e is None else "Výpadok dát trval %d min.") % round(mins)
    return {"ts_start": s, "ts_end": e, "code": "DATA", "source": "gap", "title": "Výpadok dát", "text": txt,
            "auto_key": "gap:%d" % s}


def _fc_map(con):
    return {r[0]: r[1:] for r in con.execute("SELECT code, title, description, fix FROM fault_codes")}


def _unknown_title(code, lang="sk"):
    c = str(code)
    return tr("srv.stats.neznama_chyba", lang, code=(c if re.match(r"^E\d+$", c) else ("E" + c if c.isdigit() else c)))


_GAP_RE = re.compile(r"^Výpadok dát (trvá už|trval) (\d+) min\.$")
_TCERR_RE = re.compile(r"^Chybový kód tepelného čerpadla E(\d+)\.$")


def _fault_text(text, src, lang):
    """Texty automatických porúch sa ukladajú po slovensky; na výstupe sa preložia (pre sk bez zmeny)."""
    if lang == "sk" or not text:
        return text
    m = _GAP_RE.match(text) if src == "gap" else _TCERR_RE.match(text) if src == "tc_error" else None
    if not m:
        return text
    if src == "gap":
        return tr("srv.stats.gap_trva" if m.group(1) == "trvá už" else "srv.stats.gap_trval", lang, n=m.group(2))
    return tr("srv.stats.tc_chyba", lang, code=m.group(1))


def _fault_dict(r, fc, now, lang="sk"):
    fid, s, e, code, src, title, text, akey, created = r
    known = fc.get(code)
    if known:
        t, desc, fix = known
        t, desc, fix = _seed_tr(code, 0, t, lang), _seed_tr(code, 1, desc, lang), _seed_tr(code, 2, fix, lang)
        if src == "manual" and title:
            t = title
    else:
        t, desc, fix = (title or _unknown_title(code, lang)), tr("srv.stats.doplnit_popis", lang), ""
    end = e if e is not None else None
    dur = ((e if e is not None else now) - s) / 60.0
    return {"id": fid, "ts_start": s, "ts_end": e, "code": code, "source": src, "title": t, "text": _fault_text(text, src, lang) or "",
            "description": desc or "", "fix": fix or "", "auto_key": akey, "created": created,
            "duration_min": round(max(0.0, dur), 1), "open": e is None,
            "from": time.strftime("%Y-%m-%d %H:%M", time.localtime(s)),
            "to": None if end is None else time.strftime("%Y-%m-%d %H:%M", time.localtime(end))}


_FSEL = "SELECT id, ts_start, ts_end, code, source, title, text, auto_key, created FROM faults"


def faults_list(cfg, date_from=None, date_to=None, only_open=False, lang="sk"):
    """Poruchy zasahujúce do obdobia (dátumy RRRR-MM-DD, None = bez hranice); najnovšie prvé."""
    a = _bounds(_pdate(date_from, lang))[0] if date_from else None
    b = _bounds(_pdate(date_to, lang))[1] if date_to else None
    where, args = [], []
    if b is not None:
        where.append("ts_start<?")
        args.append(b)
    if a is not None:
        where.append("(ts_end IS NULL OR ts_end>=?)")
        args.append(a)
    if only_open:
        where.append("ts_end IS NULL")
    con = connect(cfg)
    try:
        fc = _fc_map(con)
        rows = con.execute(_FSEL + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY ts_start DESC, id DESC", args).fetchall()
        now = int(time.time())
        return [_fault_dict(r, fc, now, lang) for r in rows]
    finally:
        con.close()


def _pts(v, what, lang="sk"):
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        raise ValueError(tr("srv.stats.cas_neplatny", lang, what=what))
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if re.match(r"^\d{9,11}$", s):
        return int(s)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(time.mktime(time.strptime(s, fmt)))
        except ValueError:
            pass
    raise ValueError(tr("srv.stats.cas_format", lang, what=what, s=s))


def fault_add(cfg, ts_start, ts_end, code, title="", text="", lang="sk"):
    """Ručne pridaná porucha (source 'manual'). Vracia id."""
    s = _pts(ts_start, tr("srv.stats.what_zaciatku", lang), lang)
    if s is None:
        raise ValueError(tr("srv.stats.chyba_zaciatok", lang))
    e = _pts(ts_end, tr("srv.stats.what_konca", lang), lang)
    if e is not None and e < s:
        raise ValueError(tr("srv.stats.koniec_pred", lang))
    code = str(code or "").strip().upper()
    if not re.match(r"^[A-Z0-9_.\-]{1,32}$", code):
        raise ValueError(tr("srv.stats.kod_neplatny", lang))
    title = str(title or "").strip()[:200]
    text = str(text or "").strip()[:2000]
    con = connect(cfg)
    try:
        cur = con.execute("INSERT INTO faults(ts_start, ts_end, code, source, title, text, auto_key, created) "
                          "VALUES(?,?,?,?,?,?,NULL,?)", (s, e, code, "manual", title, text, int(time.time())))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def fault_delete(cfg, fid, lang="sk"):
    """Zmaže ručne pridanú poruchu; automatické poruchy sa mazať nedajú (ValueError)."""
    try:
        fid = int(fid)
    except (TypeError, ValueError):
        raise ValueError(tr("srv.stats.id_neplatne", lang))
    con = connect(cfg)
    try:
        r = con.execute("SELECT source FROM faults WHERE id=?", (fid,)).fetchone()
        if not r:
            raise ValueError(tr("srv.stats.porucha_neexistuje", lang))
        if r[0] != "manual":
            raise ValueError(tr("srv.stats.mazat_manual", lang))
        con.execute("DELETE FROM faults WHERE id=?", (fid,))
        con.commit()
        return True
    finally:
        con.close()


def fault_codes_list(cfg, lang="sk"):
    con = connect(cfg)
    try:
        return [{"code": c, "title": _seed_tr(c, 0, t, lang), "description": _seed_tr(c, 1, d, lang) or "", "fix": _seed_tr(c, 2, f, lang) or "",
                 "editable": int(e if e is not None else 1)}
                for c, t, d, f, e in con.execute("SELECT code, title, description, fix, editable FROM fault_codes ORDER BY code")]
    finally:
        con.close()


def fault_code_set(cfg, code, title, description="", fix="", lang="sk"):
    """Vytvorí alebo upraví kód poruchy v zozname kódov. Vracia uložený záznam."""
    code = str(code or "").strip().upper()
    if not re.match(r"^[A-Z0-9_.\-]{1,32}$", code):
        raise ValueError(tr("srv.stats.kod_neplatny", lang))
    title = str(title or "").strip()[:200]
    if not title:
        raise ValueError(tr("srv.stats.nazov_prazdny", lang))
    description = str(description or "").strip()[:2000]
    fix = str(fix or "").strip()[:2000]
    con = connect(cfg)
    try:
        r = con.execute("SELECT editable FROM fault_codes WHERE code=?", (code,)).fetchone()
        if r and r[0] == 0:
            raise ValueError(tr("srv.stats.kod_needitovat", lang))
        con.execute("INSERT INTO fault_codes(code, title, description, fix, editable) VALUES(?,?,?,?,1) "
                    "ON CONFLICT(code) DO UPDATE SET title=excluded.title, description=excluded.description, fix=excluded.fix",
                    (code, title, description, fix))
        con.commit()
        return {"code": code, "title": title, "description": description, "fix": fix, "editable": 1}
    finally:
        con.close()


# ---------------------------------------------------------------- celková spotreba domu (elektromer v HA)
def apply_kwh_dom(con, vals, now=None):
    """Zapíše hodinovú spotrebu domu {začiatok_hodiny: kWh} do hourly. Dopĺňa LEN NULL (existujúca hodnota sa nikdy neprepíše):
    hodina bez riadku -> nový riadok src='ha' (n NULL); riadok z tc -> src='mix'. Vracia (vložené, doplnené)."""
    now = int(now or time.time())
    ins = upd = 0
    for ts in sorted(vals):
        v = vals[ts]
        if v is None or not v > 0:
            continue
        r = con.execute("SELECT kwh_dom FROM hourly WHERE ts=?", (ts,)).fetchone()
        if r is None:
            con.execute("INSERT INTO hourly(ts, n, src, kwh_dom, updated) VALUES(?, NULL, 'ha', ?, ?)", (ts, round(v, 4), now))
            ins += 1
        elif r[0] is None:
            con.execute("UPDATE hourly SET kwh_dom=?, src=CASE WHEN src='ha' THEN 'ha' ELSE 'mix' END WHERE ts=? AND kwh_dom IS NULL",
                        (round(v, 4), ts))
            upd += 1
    return ins, upd


def _ha_energy(cfg, a, b):
    """Hodinová spotreba domu z HA za [a, b) alebo None, ak HA nie je nastavené. Chyba spojenia = ha_ws.HAError."""
    url, token = (cfg.get("HA_URL") or "").strip(), (cfg.get("HA_TOKEN") or "").strip()
    if not url or not token:
        return None
    import ha_ws
    cli = ha_ws.HAClient()
    try:
        cli.connect(url, token, timeout=20)
        return ha_ws.energy_hourly(cli, a, b)[0]
    finally:
        cli.close()


def refresh_kwh_dom(cfg, hours=48, now=None):
    """Doťiahne posledných `hours` hodín kWh celého domu z HA (iba čítanie HA). Nikdy nevyhodí výnimku.
    Vracia {"ok","skipped","inserted","filled","error"}."""
    now = int(now or time.time())
    res = {"ok": True, "skipped": False, "inserted": 0, "filled": 0, "error": None}
    try:
        H = now - now % HOUR
        vals = _ha_energy(cfg, H - int(hours) * HOUR, H + HOUR)
        if vals is None:
            res["skipped"] = True
            return res
        con = connect(cfg)
        try:
            res["inserted"], res["filled"] = apply_kwh_dom(con, vals, now)
            con.commit()
        finally:
            con.close()
    except Exception as e:
        res["ok"] = False
        res["error"] = "%s: %s" % (type(e).__name__, e)
    return res


# ---------------------------------------------------------------- kalibrácia odhadu spotreby čerpadla
def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def _theil_sen(pts, max_pts=200, min_dx=0.15):
    """Robustná priamka y = b0 + b1·x metódou Theil–Sen (medián sklonov dvojíc s |Δx| > min_dx, potom medián y − b1·x); odolná voči
    výpadkom počítadla a hodinám s výnimočnou spotrebou domu. Pri > max_pts bodoch sa body rovnomerne preriedia.
    Vracia (b1, b0) alebo None, ak x nevarieje."""
    if len(pts) > max_pts:
        step = len(pts) / float(max_pts)
        pts = [pts[int(i * step)] for i in range(max_pts)]
    sl = []
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            dx = pts[j][0] - pts[i][0]
            if abs(dx) > min_dx:
                sl.append((pts[j][1] - pts[i][1]) / dx)
    if len(sl) < 3:
        return None
    b1 = _median(sl)
    return b1, _median([y - b1 * x for x, y in pts])


def _ts_intercept(pts, max_pts=200):
    """Úsek priamky (b0) z robustného odhadu Theil–Sen alebo None, ak x nevarieje."""
    r = _theil_sen(pts, max_pts)
    return r[1] if r else None


def _night_fit(rows, now):
    """Kalibrácia z nočných hodín (lokálna hodina 0–4, posledných CAL_DAYS dní, beh >= 20 min, kwh >= 0,3 s údajom z elektromera):
    dom ≈ b0 + k·kwh. Vracia {"k_raw", "b0", "pts", "n_hodin", "n_dni"} alebo None, ak podmienky nie sú splnené."""
    pts, days = [], set()
    for ts, beh, kwh, dom in rows:
        if dom is None or beh is None or kwh is None or ts < now - CAL_DAYS * 86400:
            continue
        if time.localtime(ts).tm_hour < 5 and beh >= NIGHT_MIN_RUN and kwh >= NIGHT_MIN_KWH:
            pts.append((kwh, dom))
            days.add(_dstr(ts))
    if len(pts) < NIGHT_MIN_N or len(days) < CAL_MIN_DAYS or max(p[0] for p in pts) - min(p[0] for p in pts) < NIGHT_MIN_SPAN:
        return None
    ts_fit = _theil_sen(pts, 200, 0.3)
    if ts_fit is None:
        return None
    return {"k_raw": ts_fit[0], "b0": ts_fit[1], "pts": pts, "n_hodin": len(pts), "n_dni": len(days)}


NB_DAYS, NB_MIN_N, NB_WARM = 400, 20, 15.0   # nočný základ domu: koľko dní dozadu, min. hodín, „teplá noc“ od tejto teploty vonku


def night_base(con, now=None):
    """Zmeraná spotreba domu v noci bez čerpadla (kWh za hodinu): medián kwh_dom v hodinách 0–4 za posledných NB_DAYS dní,
    keď čerpadlo nebežalo (beh_min = 0, alebo údaj o chode chýba a vonku bolo >= 15 °C, teda sa nekúrilo). Medián odfiltruje
    občasný nočný ohrev TÚV. Vracia (hodnota, počet hodín) alebo (None, počet), ak je hodín málo."""
    now = int(now or time.time())
    xs = []
    for ts, dom, beh, vonku in con.execute("SELECT ts, kwh_dom, beh_min, vonku FROM hourly WHERE ts>=? AND ts<? AND kwh_dom IS NOT NULL",
                                           (now - NB_DAYS * 86400, now)):
        if time.localtime(ts).tm_hour >= 5:
            continue
        if (beh is not None and beh <= 0.01) or (beh is None and vonku is not None and vonku >= NB_WARM):
            xs.append(dom)
    if len(xs) < NB_MIN_N:
        return None, len(xs)
    return round(_median(xs), 3), len(xs)


def calibration_calc(rows, now, nbase=None):
    """Čistý výpočet kalibrácie. rows = [(ts, beh_min, kwh, kwh_dom)], now = unix s. Vracia slovník (k môže byť None).
    Základná spotreba domu: medián kwh_dom podľa hodiny dňa z hodín posledných 30 dní bez chodu čerpadla (beh_min = 0, kwh ~ 0;
    beh_min NULL sa nepoužije); pri < 5 hodinách pre danú hodinu medián nočných hodín bez chodu; ak čerpadlo bežalo nepretržite
    (žiadna hodina bez chodu), záloha = konštantný úsek robustnej regresie (Theil–Sen) kwh_dom ~ kwh. k = (metoda "noc") sklon
    Theil–Sen kwh_dom ~ kwh z nočných hodín 0–4 (_night_fit), ak ich je dosť; inak (metoda "dni") medián denných podielov
    Σ max(0, kwh_dom − základ) / Σ kwh (dni s >= 6 hodinami chodu >= 50 min). k sa obmedzí na 0,6–1,5."""
    base_from = now - CAL_BASE_DAYS * 86400
    idle, idle_all = {}, []
    run = []
    for ts, beh, kwh, dom in rows:
        if dom is None or beh is None:
            continue
        h = time.localtime(ts).tm_hour
        if ts >= base_from and beh <= 0.01 and (kwh is None or kwh < 0.05):
            idle.setdefault(h, []).append(dom)
            idle_all.append((h, dom))
        if beh >= CAL_MIN_RUN_MIN and kwh is not None and kwh >= COP_MIN_KWH:
            run.append((ts, h, kwh, dom))
    out = {"k": None, "k_raw": None, "n_dni": 0, "n_hodin": 0, "mape": None, "base_hodin": len(idle_all),
           "base_zdroj": None, "base_kwh": None, "zdroj": "elektromer", "dovod": None, "ts": int(now),
           "metoda": "dni", "noc_zaklad_kwh": None}
    nf = _night_fit(rows, now)
    if nf:
        raw, b0, met = nf["k_raw"], nf["b0"], "noc"
        if nbase is not None and nbase > 0:
            # nočný základ domu je zmeraný (teplé noci bez kúrenia): k = medián podielov (elektromer − základ) / odhad;
            # voľný úsek regresie by časť základu domu pripísal čerpadlu
            raw, b0, met = _median([(y - nbase) / x for x, y in nf["pts"]]), nbase, "noc_zaklad"
        k = min(max(raw, K_LO), K_HI)
        errs = [abs(b0 + k * x - y) / y for x, y in nf["pts"] if y > 0]
        out.update({"metoda": met, "k_raw": round(raw, 4), "k": round(k, 4), "n_hodin": nf["n_hodin"],
                    "n_dni": nf["n_dni"], "noc_zaklad_kwh": round(b0, 3),
                    "mape": round(100.0 * sum(errs) / len(errs), 1) if errs else None})
        if k != raw:
            out["dovod"] = "Vypočítaný podiel %s je mimo rozsahu %s–%s, použitá hranica." % (round(raw, 3), K_LO, K_HI)
    base = {}
    night = [d for h, d in idle_all if h < 6]
    fb = _median(night) if night else _median([d for _, d in idle_all])
    if fb is not None:
        nfb = 0
        for h in range(24):
            if len(idle.get(h, ())) >= CAL_MIN_BASE_N:
                base[h] = _median(idle[h])
            else:
                base[h] = fb
                nfb += 1
        out["base_zdroj"] = "median hodín bez chodu" + (" (pre %d hodín dňa náhradný nočný medián)" % nfb if nfb else "")
    elif len(run) >= 12:
        b0 = _ts_intercept([(r[2], r[3]) for r in run])
        if b0 is not None:
            b0 = min(max(b0, 0.0), 1.5)
            base = {h: b0 for h in range(24)}
            out["base_zdroj"] = "regresia Theil–Sen (čerpadlo bežalo nepretržite, chýbajú hodiny bez chodu)"
    if not base:
        if not nf:
            out["dovod"] = "Chýbajú hodiny bez chodu čerpadla aj dosť údajov na odhad základnej spotreby domu."
        return out
    out["base_kwh"] = round(_median(list(base.values())), 3)
    if nf:                                      # k je už určené z nočných hodín
        return out
    days = {}
    for ts, h, kwh, dom in run:
        x = days.setdefault(_dstr(ts), [0.0, 0.0, 0])
        x[0] += max(0.0, dom - base[h])
        x[1] += kwh
        x[2] += 1
    used = {d: x for d, x in days.items() if x[2] >= CAL_MIN_DAY_HOURS and x[1] > 0}
    out["n_dni"] = len(used)
    out["n_hodin"] = sum(x[2] for x in used.values())
    if len(used) < CAL_MIN_DAYS:
        out["dovod"] = ("Zatiaľ málo údajov: potrebné sú aspoň %d dni s min. %d hodinami chodu kompresora (>= %d min) a údajom z elektromera, "
                        "k dispozícii %d." % (CAL_MIN_DAYS, CAL_MIN_DAY_HOURS, CAL_MIN_RUN_MIN, len(used)))
        return out
    raw = _median([x[0] / x[1] for x in used.values()])
    k = min(max(raw, K_LO), K_HI)
    errs = [abs(k * x[1] - x[0]) / x[0] for x in used.values() if x[0] > 0]
    out["k_raw"] = round(raw, 4)
    out["k"] = round(k, 4)
    out["mape"] = round(100.0 * sum(errs) / len(errs), 1) if errs else None
    if k != raw:
        out["dovod"] = "Vypočítaný podiel %s je mimo rozsahu %s–%s, použitá hranica." % (round(raw, 3), K_LO, K_HI)
    return out


def get_cal(con):
    """Uložená kalibrácia z meta (slovník alebo None)."""
    try:
        r = con.execute("SELECT v FROM meta WHERE k='kalibracia'").fetchone()
        d = json.loads(r[0]) if r and r[0] else None
    except (sqlite3.Error, ValueError):
        return None
    return d if isinstance(d, dict) else None


def get_k(con):
    d = get_cal(con)
    k = d.get("k") if d else None
    return float(k) if isinstance(k, (int, float)) and K_LO <= k <= K_HI else None


def recalibrate(cfg, now=None):
    """Prepočíta kalibráciu, uloží ju do meta (JSON `kalibracia`) a prepíše kwh_cal a cop v hodinách posledných 60 dní. Potom
    prekalibruje prietok okruhov (flow_calibrate, meta `prietok`) a ak sa k alebo prietok zmenili, prepočíta hodinové riadky
    (q_kwh, cop) za obdobie s minútovými dátami v tc.db (najviac 60 dní; kwh_dom sa nedotkne). Vracia slovník s kľúčom `prietok`."""
    now = int(now or time.time())
    t0 = time.time()
    H = now - now % HOUR
    con = connect(cfg)
    try:
        old_k, old_fl = get_k(con), get_flows(con)
        rows = con.execute("SELECT ts, beh_min, kwh, kwh_dom FROM hourly WHERE ts>=? AND ts<?",
                           (H - CAL_DAYS * 86400, H)).fetchall()
        nb, nb_n = night_base(con, now)
        res = calibration_calc(rows, now, nb)
        res["noc_zaklad_n"] = nb_n
        res["datum"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
        res["trvanie_s"] = round(time.time() - t0, 2)
        k = res["k"]
        a = H - CAL_DAYS * 86400
        if k:
            con.execute("UPDATE hourly SET kwh_cal=ROUND(kwh*?, 3) WHERE ts>=? AND kwh IS NOT NULL", (k, a))
        else:
            con.execute("UPDATE hourly SET kwh_cal=NULL WHERE ts>=?", (a,))
        con.execute("UPDATE hourly SET cop=CASE WHEN q_kwh IS NOT NULL AND COALESCE(kwh_cal, kwh) >= ? "
                    "THEN ROUND(q_kwh / COALESCE(kwh_cal, kwh), 3) ELSE NULL END WHERE ts>=? AND q_kwh IS NOT NULL",
                    (COP_MIN_KWH, a))
        _meta_set(con, "kalibracia", json.dumps(res, ensure_ascii=False))
        con.commit()
    finally:
        con.close()
    # prietok okruhov (číta nové k z meta), potom prípadný prepočet hodín; aggregate_range volá len agregáciu, nie recalibrate
    res["prietok"] = flow_calibrate(cfg, now)
    try:
        con = connect(cfg)
        try:
            new_fl = get_flows(con)
        finally:
            con.close()
        chg = (old_k is None) != (k is None) or (k is not None and abs(k - old_k) > 0.005)
        for key in ("room", "tank"):
            o, n = old_fl.get(key), new_fl.get(key)
            if (o is None) != (n is None) or (n is not None and abs(n - o) > 0.3):
                chg = True
        if chg:
            tc = _tc(cfg)
            try:
                first = tc.execute("SELECT min(ts) FROM samples").fetchone()[0]
            finally:
                tc.close()
            if first is not None:
                a = max(H - 60 * 86400, first - first % HOUR)
                res["prepocet_hodin"] = aggregate_range(cfg, a, H + HOUR)
    except Exception as e:                       # kalibrácia je uložená; prepočet sa zopakuje pri ďalšej zmene / dennom prepočte
        res["prepocet_chyba"] = "%s: %s" % (type(e).__name__, e)
    return res


# ---------------------------------------------------------------- živé hodnoty (COP, príkon) pre /api/status
def live(cfg, now=None):
    """Rýchle živé hodnoty z minútových vzoriek tc.db (iba čítanie): {cop_now, p_th_kw, p_el_w, cop_day, kwh_day, kwh_day_dom,
    kwh_zdroj, k, updated_ts, q_ok, flow_lpm, flow_zdroj}. cop_now = posledných 15 min (iba bežiaci kompresor a kladné teplo, inak
    None); denné hodnoty od lokálnej polnoci. flow_lpm/flow_zdroj = prietok použitý v poslednej minúte ("senzor"|"zmerany"|"tabulky")."""
    now = int(now or time.time())
    out = {"cop_now": None, "p_th_kw": None, "p_el_w": None, "cop_day": None, "kwh_day": None, "kwh_day_dom": None,
           "kwh_zdroj": "odhad", "k": None, "updated_ts": None, "q_ok": False, "flow_lpm": None, "flow_zdroj": None}
    k = None
    dom = None
    cal_flows = None
    p = _db_path(cfg)
    if os.path.exists(p):
        try:
            sc = _ro(p, _timeout(cfg))
            try:
                k = get_k(sc)
                cal_flows = get_flows(sc)
                a0 = _midnight(_dt.date.fromtimestamp(now))
                r = sc.execute("SELECT sum(kwh_dom), count(*) FROM hourly WHERE ts>=? AND kwh_dom IS NOT NULL", (a0,)).fetchone()
                dom = round(r[0], 2) if r and r[1] else None
            finally:
                sc.close()
        except sqlite3.Error:
            pass
    out["k"] = k
    out["kwh_zdroj"] = "kalibrovany" if k else "odhad"
    out["kwh_day_dom"] = dom
    kk = k or 1.0
    a0 = _midnight(_dt.date.fromtimestamp(now))
    fixed = cfg_flows(cfg)
    tc = _tc(cfg)
    try:
        valve = _tracks(tc, a0, now + 1)[0]
        ents = [TC[x] for x in ("freq", "tw2", "tw1", "flow", "prikon", "kwh") if x in TC]
        by = {}
        last = None
        for ts, ent, v in tc.execute("SELECT ts, entity, value FROM samples WHERE ts>=? AND entity IN (%s) ORDER BY ts"
                                     % ",".join("?" * len(ents)), (a0, *ents)):
            if v is None:
                continue
            by.setdefault(INV[ent], {})[ts] = v
            last = ts
        if last is None:
            return out
        out["updated_ts"] = last
        pk = by.get("prikon", {})
        if pk:
            out["p_el_w"] = round(pk[max(pk)] * kk, 0)
        fq, t2, t1, fl = by.get("freq", {}), by.get("tw2", {}), by.get("tw1", {}), by.get("flow", {})
        qd = ed = 0.0                # denný súčet tepla a elektriny v minútach s platným prietokom [kWh]
        qn = en = 0.0                # posledných 15 min
        nn = 0
        thp = []
        for ts, f in fq.items():
            if f <= 0 or ts not in t2 or ts not in t1 or ts not in pk:
                continue
            fv, fz = flow_for(valve.at(ts), fl.get(ts), fixed, cal_flows)
            if fv is None:
                continue
            out["flow_lpm"], out["flow_zdroj"] = round(fv, 1), fz
            out["q_ok"] = True
            q = pth_kw(fv, t2[ts], t1[ts])
            qd += q / 60.0
            ed += pk[ts] * kk / 60000.0
            if ts > last - 900:
                qn += q / 60.0
                en += pk[ts] * kk / 60000.0
                thp.append(q)
                nn += 1
        if ed >= COP_MIN_KWH:
            out["cop_day"] = round(qd / ed, 2)
        if nn and en > 0 and qn > 0:
            out["cop_now"] = round(qn / en, 2)
            out["p_th_kw"] = round(sum(thp) / len(thp), 2)
        kc = by.get("kwh", {})
        if kc:
            tot, pv = 0.0, None
            for ts in sorted(kc):
                v = kc[ts]
                if pv is not None and 0 < v - pv < 2:
                    tot += v - pv
                pv = v
            out["kwh_day"] = round(tot * kk, 2)
        return out
    finally:
        tc.close()


# ---------------------------------------------------------------- doplnenie, slučka
def recompute_range(cfg, a, b, now=None, ongoing=False):
    """Hodiny, cykly aj poruchy pre [a, b). Vracia počet hodinových riadkov."""
    n = aggregate_range(cfg, a, b)
    update_cycles(cfg, a, now)
    detect_faults(cfg, a, now, ongoing)
    return n


def catch_up(cfg, now=None, ongoing=False, include_open=True):
    """Doplní hodinové riadky od najstaršej vzorky tc.db po poslednú uzavretú hodinu (a rozpracovanú hodinu, ak include_open),
    ktoré v hourly chýbajú alebo sú staršie než posledná vzorka tej hodiny; posledné 3 uzavreté hodiny vždy prepočíta.
    Následne obnoví cykly a poruchy. Vracia počet prepočítaných hodín."""
    now = int(now or time.time())
    H = now - now % HOUR
    last_closed = H - HOUR
    end_hour = H if include_open else last_closed
    tc = _tc(cfg)
    try:
        tch = dict(tc.execute("SELECT ts - ts % 3600, max(ts) FROM samples GROUP BY 1").fetchall())
    finally:
        tc.close()
    if not tch:
        return 0
    con = connect(cfg)
    try:
        ex = {ts: (upd, src) for ts, upd, src in con.execute("SELECT ts, updated, src FROM hourly")}
    finally:
        con.close()
    todo = set()
    for h, mx in tch.items():
        if h > end_hour:
            continue
        e = ex.get(h)
        if e is None or e[0] is None or e[0] < mx or e[1] == "ha":
            todo.add(h)
    for h in range(last_closed - 2 * HOUR, end_hour + 1, HOUR):
        if h in tch:
            todo.add(h)
    if not todo:
        return 0
    hs = sorted(todo)
    written = 0
    i = 0
    while i < len(hs):
        j = i
        while j + 1 < len(hs) and hs[j + 1] == hs[j] + HOUR:
            j += 1
        written += aggregate_range(cfg, hs[i], hs[j] + HOUR)
        i = j + 1
    start = hs[0]
    update_cycles(cfg, start, now)
    detect_faults(cfg, start, now, ongoing)
    con = connect(cfg)
    try:
        _meta_set(con, "last_hour_done", last_closed)
        _meta_set(con, "last_catch_up", now)
        con.commit()
    finally:
        con.close()
    return written


def stats_tick(cfg, st=None, now=None):
    """Jedna iterácia slučky. Prvé volanie (prázdne st) robí catch_up; ďalšie prepočítajú posledné 3 uzavreté hodiny a rozpracovanú;
    raz denne po 03:40 posledných 7 dní. Nikdy nevyhodí výnimku; vracia {"ok","hours","error"}."""
    st = st if st is not None else {}
    now = int(now or time.time())
    res = {"ok": True, "hours": 0, "error": None}
    try:
        if not st.get("started"):
            res["hours"] += catch_up(cfg, now=now, ongoing=True)
            st["started"] = True
        else:
            H = now - now % HOUR
            res["hours"] += recompute_range(cfg, H - 3 * HOUR, H + HOUR, now=now, ongoing=True)
        lt = time.localtime(now)
        today = time.strftime("%Y-%m-%d", lt)
        if (lt.tm_hour, lt.tm_min) >= (3, 40) and st.get("daily") != today:
            H = now - now % HOUR
            res["hours"] += recompute_range(cfg, H - 7 * 86400, H + HOUR, now=now, ongoing=True)
            st["daily"] = today
        st["last_hour"] = now - now % HOUR
        # spotreba celého domu z HA (raz za hodinu, posledných 48 h); kalibrácia raz denne a ak chýba / je stará
        H = now - now % HOUR
        if st.get("dom_hour") != H:
            st["dom_hour"] = H
            r = refresh_kwh_dom(cfg, 48, now)
            if r["error"]:
                if not st.get("dom_err"):                         # zaloguj raz, kým problém trvá
                    print("stats: kwh_dom z Home Assistanta nedostupné (%s)" % r["error"][:200], flush=True)
                st["dom_err"] = True
            else:
                st["dom_err"] = False
        cal_due = st.get("cal_day") != today and (lt.tm_hour, lt.tm_min) >= (3, 50)
        if not st.get("cal_checked"):
            st["cal_checked"] = True
            con = connect(cfg)
            try:
                c = get_cal(con)
            finally:
                con.close()
            cal_due = cal_due or not c or now - int(c.get("ts") or 0) > 20 * 3600
        if cal_due:
            st["cal_day"] = today
            recalibrate(cfg, now)
    except Exception as e:                       # nikdy nezhodí agenta
        res["ok"] = False
        res["error"] = "%s: %s" % (type(e).__name__, e)
    return res


def stats_loop():
    """Daemon vlákno agenta: pri štarte catch_up, potom každých 5 min. Nikdy nespadne, nezapisuje do tc.db ani agent.db."""
    time.sleep(20)
    st = {}
    while True:
        try:
            cfg = load_cfg()
            r = stats_tick(cfg, st)
            if not r["ok"]:
                print("stats: %s" % r["error"], flush=True)
        except Exception:
            try:
                traceback.print_exc()
            except Exception:
                pass
        time.sleep(300)


# ---------------------------------------------------------------- súhrny
def _has(r):
    n = r.get("n")
    return n is None or n > 0


def _summarize(rows, base):
    rows = [r for r in rows if _has(r)]
    s = {}
    for m in METRICS:
        k, agg = m["key"], m["agg"]
        if agg == "ratio":                       # podiel súčtov, počíta sa nižšie (cop)
            continue
        vals = [r for r in rows if r.get(k) is not None]
        if not vals:
            s[k] = None
            continue
        if agg == "sum":
            v = sum(r[k] for r in vals)
        elif agg == "max":
            v = max(r[k] for r in vals)
        else:
            if k == "hz_avg":
                ws = [r.get("beh_min") or 0 for r in vals]
            elif k == "n":
                ws = [1] * len(vals)
            else:
                ws = [r.get("n") or 1 for r in vals]
            if not sum(ws):
                ws = [1] * len(vals)
            v = sum(r[k] * w for r, w in zip(vals, ws)) / float(sum(ws))
        s[k] = int(round(v)) if k == "starty" else round(v, 2)

    def ext(k, fn):
        xs = [r[k] for r in rows if r.get(k) is not None]
        return round(fn(xs), 2) if xs else None
    s["vonku_min"] = ext("vonku_min", min)
    s["vonku_max"] = ext("vonku_max", max)
    s["tuv_max"] = ext("tuv", max)
    bh = [r for r in rows if r.get("beh_min") is not None]
    s["beh_pct"] = round(100.0 * sum(r["beh_min"] for r in bh) / (60.0 * len(bh)), 1) if bh else None
    vh = [r["vonku"] for r in rows if r.get("vonku") is not None]
    s["hdd"] = round(sum(max(0.0, base - v) for v in vh) / 24.0, 2) if vh else None
    # spotreba čerpadla: kalibrovaná (kwh_cal), ak je, inak odhad (kwh); zdroj podľa podielu kalibrovaných hodnôt
    kr = [r for r in rows if r.get("kwh") is not None]
    tot = sum(_den(r) for r in kr)
    cal = sum(r["kwh_cal"] for r in kr if r.get("kwh_cal") is not None)
    s["kwh_spotreba"] = round(tot, 2) if kr else None
    s["kwh_zdroj"] = "kalibrovany" if kr and tot > 0 and cal >= 0.9 * tot else "odhad"
    # COP = Σ vyrobené teplo / Σ spotreba cez hodiny, kde teplo je známe (nie priemer hodinových COP)
    s["cop"], s["cop_hodin"] = _cop_of([r for r in rows if r.get("q_kwh") is not None])
    pure_k = [r for r in rows if _pure(r, "kurenie_min")]
    pure_t = [r for r in rows if _pure(r, "tuv_min")]
    s["cop_kurenie"], s["cop_kurenie_hodin"] = _cop_of(pure_k)
    s["cop_tuv"], s["cop_tuv_hodin"] = _cop_of(pure_t)
    s["kwh_hdd"] = (round(s["kwh_spotreba"] / s["hdd"], 2) if (s["hdd"] is not None and s["hdd"] >= 2 and len(rows) >= 18
                                                               and s["kwh_spotreba"] is not None) else None)
    s["hodin_s_datami"] = len(rows)
    return s


def _cop_of(rows):
    q = d = 0.0
    n = 0
    for r in rows:
        den = _den(r)
        if r.get("q_kwh") is None or den is None or den < COP_MIN_KWH:
            continue
        q += r["q_kwh"]
        d += den
        n += 1
    return (round(q / d, 2), n) if n and d > 0 else (None, 0)


def _pure(r, key):
    """Hodina s chodom >= 30 min, z ktorého aspoň 90 % tvorí jeden režim (kúrenie alebo ohrev TÚV; zvyšok napr. odmrazovanie)."""
    b, m = r.get("beh_min"), r.get(key)
    return b is not None and m is not None and b >= PURE_RUN_MIN and m >= PURE_SHARE * b


_SEL = ""


def _hourly(con, a, b):
    out = []
    for r in con.execute(_SEL + " WHERE ts>=? AND ts<? ORDER BY ts", (a, b)):
        d = {"ts": r[0], "n": r[1], "src": r[2]}
        d.update(zip(HCOLS, r[3:]))
        out.append(d)
    return out


def _count_faults(con, a, b, now=None):
    now = int(now or time.time())
    return con.execute("SELECT count(*) FROM faults WHERE ts_start<? AND COALESCE(ts_end, ?)>=?", (b, now, a)).fetchone()[0]


# ---------------------------------------------------------------- časová os dňa
def _timeline(cfg, con, a, b):
    segs = None
    try:
        tc = _tc(cfg)
        try:
            fr = tc.execute("SELECT ts, value FROM samples WHERE entity=? AND ts>=? AND ts<? AND value IS NOT NULL ORDER BY ts",
                            (TC.get("freq", ""), a, b)).fetchall()
            if fr:
                valve, defr = _tracks(tc, a, b)
                segs = []
                for i, (ts, v) in enumerate(fr):
                    nxt = fr[i + 1][0] if i + 1 < len(fr) else None
                    span = (nxt - ts) if nxt is not None and nxt - ts <= 330 else 60
                    if v > 0:
                        st = "odmrazovanie" if defr.at(ts) else ("beh_tuv" if valve.at(ts) == "tank" else "beh_kurenie")
                    else:
                        st = "stoji"
                    segs.append((ts, min(ts + span, b), st))
        finally:
            tc.close()
    except Exception:
        segs = None
    if segs is None:
        segs = []
        mp = {"kurenie": "beh_kurenie", "tuv": "beh_tuv", "odmrazovanie": "odmrazovanie", "zmiesane": "beh_kurenie"}
        for s, e, mode in con.execute("SELECT ts_start, COALESCE(ts_stop, ts_start + CAST(minutes*60 AS INTEGER)), mode FROM cycles "
                                      "WHERE ts_start<? AND COALESCE(ts_stop, ts_start + CAST(minutes*60 AS INTEGER))>? ORDER BY ts_start",
                                      (b, a)):
            segs.append((max(s, a), min(e, b), mp.get(mode, "beh_kurenie")))
    out = []
    pos = a
    for s, e, st in sorted(segs):
        s = max(s, a, pos)
        if e <= s:
            continue
        if s > pos:
            _tl_add(out, pos, s, "bez_dat")
        _tl_add(out, s, e, st)
        pos = e
    if pos < b:
        _tl_add(out, pos, b, "bez_dat")
    return out


def _tl_add(out, s, e, st):
    if out and out[-1]["state"] == st and out[-1]["to"] == s:
        out[-1]["to"] = e
    else:
        out.append({"from": s, "to": e, "state": st})


# ---------------------------------------------------------------- dotazové funkcie
def coverage(cfg, lang="sk"):
    con = connect(cfg)
    try:
        def first(cond):
            r = con.execute("SELECT min(ts) FROM hourly WHERE (n IS NULL OR n>0) AND (%s)" % cond).fetchone()
            return _dstr(r[0]) if r and r[0] is not None else None
        rooms = " OR ".join("%s IS NOT NULL" % k for k in ROOM_KEYS)
        r = con.execute("SELECT max(ts), count(*), count(DISTINCT date(ts,'unixepoch','localtime')) FROM hourly "
                        "WHERE (n IS NULL OR n>0)").fetchone()
        fd = {"vonku": first("vonku IS NOT NULL"), "izby": first(rooms), "voda": first("tw2 IS NOT NULL"),
              "kompresor": first("beh_min IS NOT NULL"), "energia": first("kwh IS NOT NULL"),
              "kwh_dom": first("kwh_dom IS NOT NULL"), "cop": first("cop IS NOT NULL")}
        cal = get_cal(con) or {}
        last_dom = con.execute("SELECT max(ts) FROM hourly WHERE kwh_dom IS NOT NULL").fetchone()[0]
        kal = {"k": cal.get("k"), "n_dni": cal.get("n_dni", 0), "n_hodin": cal.get("n_hodin", 0), "mape": cal.get("mape"),
               "ts": cal.get("ts"), "datum": cal.get("datum"), "zdroj": cal.get("zdroj") if cal.get("k") else None,
               "dovod": cal.get("dovod"), "base_kwh": cal.get("base_kwh"), "base_zdroj": cal.get("base_zdroj"),
               "metoda": cal.get("metoda"), "noc_zaklad_kwh": cal.get("noc_zaklad_kwh"), "prietok": get_prietok(con),
               "vypocitane": bool(cal)}
        return {"first_day": fd, "prietok": kal["prietok"],
                "last_day": _dstr(r[0]) if r[0] is not None else None, "days_with_data": r[2] or 0, "hours": r[1] or 0,
                "kalibracia": kal, "kwh_dom_do": _dstr(last_dom) if last_dom is not None else None,
                "cop_zdroj": ("pevny_prietok" if cfg_flow(cfg) else "prietok") if fd["cop"] else None,
                "cop_poznamka": None if fd["cop"] else
                tr("srv.stats.cop_poznamka", lang)}
    finally:
        con.close()


def get_day(cfg, date, lang="sk"):
    d = _pdate(date, lang)
    a, b = _bounds(d)
    con = connect(cfg)
    try:
        rows = _hourly(con, a, b)
        rm = {r["ts"]: r for r in rows}
        hours = []
        for ts in range(a, b, HOUR):
            r = rm.get(ts)
            e = {"ts": ts, "h": time.localtime(ts).tm_hour, "src": r["src"] if r else None}
            for k in METRIC_KEYS + ["vonku_min", "vonku_max"]:
                e[k] = r.get(k) if r else None
            hours.append(e)
        summ = _summarize(rows, _hbase(cfg))
        summ["date"] = d.isoformat()
        summ["poruch"] = _count_faults(con, a, b)
        now = int(time.time())
        cycles = []
        for r in con.execute("SELECT id, ts_start, ts_stop, minutes, f_max, f_avg, mode, vonku, tw2_max, reason FROM cycles "
                             "WHERE ts_start<? AND COALESCE(ts_stop, ?)>=? ORDER BY ts_start", (b, now, a)):
            c = dict(zip(("id", "ts_start", "ts_stop", "minutes", "f_max", "f_avg", "mode", "vonku", "tw2_max", "reason"), r))
            c["running"] = c["ts_stop"] is None
            c["reason"] = _reason(c["reason"], lang)
            cycles.append(c)
        fc = _fc_map(con)
        faults = [_fault_dict(r, fc, now, lang) for r in con.execute(
            _FSEL + " WHERE ts_start<? AND COALESCE(ts_end, ?)>=? ORDER BY ts_start, id", (b, now, a))]
        return {"date": d.isoformat(), "hours": hours, "summary": summ, "cycles": cycles, "faults": faults,
                "timeline": _timeline(cfg, con, a, b), "has_data": summ["hodin_s_datami"] > 0,
                "kwh_zdroj": summ["kwh_zdroj"], "kalibracia_k": get_k(con), "prietok": get_prietok(con)}
    finally:
        con.close()


_REASONS = {"odmrazovanie": "defrost", "TÚV": "dhw", "dosiahnutý cieľ": "target", "iné": "other"}


def _reason(r, lang):
    """Dôvod ukončenia cyklu sa ukladá po slovensky; na výstupe sa pre iný jazyk preloží."""
    if lang == "sk" or r not in _REASONS:
        return r
    return tr("srv.stats.reason_" + _REASONS[r], lang)


def _period(d, group):
    if group == "day":
        return d.isoformat(), d, d
    if group == "week":
        y, w, wd = d.isocalendar()
        mon = d - _dt.timedelta(days=wd - 1)
        return "%d-W%02d" % (y, w), mon, mon + _dt.timedelta(days=6)
    first = d.replace(day=1)
    nxt = (first + _dt.timedelta(days=32)).replace(day=1)
    return "%d-%02d" % (d.year, d.month), first, nxt - _dt.timedelta(days=1)


def get_range(cfg, date_from, date_to, group="day", lang="sk"):
    d1, d2 = _pdate(date_from, lang), _pdate(date_to, lang)
    if group not in ("day", "week", "month"):
        raise ValueError(tr("srv.stats.zoskupenie", lang))
    if d2 < d1:
        raise ValueError(tr("srv.stats.do_pred_od", lang))
    if (d2 - d1).days > 366 * 40:
        raise ValueError(tr("srv.stats.rozsah_velky", lang))
    periods = []
    d = d1
    while d <= d2:
        key, pf, pt = _period(d, group)
        if not periods or periods[-1][0] != key:
            periods.append([key, max(pf, d1), min(pt, d2)])
            if len(periods) > 800:
                raise ValueError(tr("srv.stats.rozsah_800", lang))
        d += _dt.timedelta(days=1)
    a, b = _bounds(d1)[0], _bounds(d2)[1]
    con = connect(cfg)
    try:
        rows = _hourly(con, a, b)
        base = _hbase(cfg)
        now = int(time.time())
        fl = con.execute("SELECT ts_start, ts_end FROM faults WHERE ts_start<? AND COALESCE(ts_end, ?)>=?", (b, now, a)).fetchall()
        bykey = {}
        for r in rows:
            key = _period(_dt.date.fromtimestamp(r["ts"]), group)[0]
            bykey.setdefault(key, []).append(r)
        out = []
        for key, pf, pt in periods:
            pa, pb = _bounds(pf)[0], _bounds(pt)[1]
            rr = bykey.get(key, [])
            s = _summarize(rr, base)
            days = len({time.strftime("%Y-%m-%d", time.localtime(r["ts"])) for r in rr if _has(r)})
            s["poruch"] = sum(1 for fs, fe in fl if fs < pb and (fe if fe is not None else now) >= pa)
            row = {"key": key, "from": pf.isoformat(), "to": pt.isoformat()}
            row.update(s)
            row["dni_s_datami"] = days
            out.append(row)
        tot = _summarize(rows, base)
        tot["poruch"] = len(fl)
        tot["dni_s_datami"] = len({time.strftime("%Y-%m-%d", time.localtime(r["ts"])) for r in rows if _has(r)})
        tot["dni"] = (d2 - d1).days + 1
        return {"group": group, "rows": out, "totals": tot}
    finally:
        con.close()


def _label(d, today, lang="sk"):
    diff = (today - d).days
    fixed = {0: "dnes", 1: "vcera", 7: "tyzden", 14: "tyzdne2", 30: "mesiac"}
    if diff in fixed:
        return tr("srv.stats.lbl_" + fixed[diff], lang)
    for n in (1, 2, 3):
        if d == _shift_years(today, n):
            return tr("srv.stats.lbl_rok%d" % n, lang)
    return _slk(d, lang)


def _merge_hours(hours):
    """24 záznamov podľa lokálnej hodiny 0–23 (pri 25-hodinovom dni sa zdvojená hodina zlúči, pri 23-hodinovom chýbajúca je prázdna)."""
    by = {}
    for e in hours:
        by.setdefault(e["h"], []).append(e)
    out = []
    for h in range(24):
        e = {"h": h}
        grp = by.get(h, [])
        for m in METRICS:
            k = m["key"]
            vals = [g[k] for g in grp if g.get(k) is not None]
            if not vals:
                e[k] = None
            elif m["agg"] == "sum":
                e[k] = round(sum(vals), 2)
            elif m["agg"] == "max":
                e[k] = max(vals)
            else:
                e[k] = round(sum(vals) / len(vals), 2)
        out.append(e)
    return out


def compare(cfg, dates, lang="sk"):
    if isinstance(dates, str):
        dates = [x for x in re.split(r"[,\s]+", dates) if x]
    if not isinstance(dates, (list, tuple)):
        raise ValueError(tr("srv.stats.datumy_zoznam", lang))
    ds = []
    for x in dates:
        d = _pdate(x, lang)
        if d not in ds:
            ds.append(d)
    if not 1 <= len(ds) <= 6:
        raise ValueError(tr("srv.stats.porovnat", lang))
    today = _today()
    out = []
    for d in ds:
        day = get_day(cfg, d.isoformat(), lang)
        out.append({"date": d.isoformat(), "label": _label(d, today, lang), "has_data": day["has_data"],
                    "summary": day["summary"], "hours": _merge_hours(day["hours"])})
    return {"dates": out, "metrics": metrics(lang)}


QUICK = (("-1d", "Včera", 1, 0), ("-7d", "Pred týždňom", 7, 0), ("-14d", "Pred 2 týždňami", 14, 0),
         ("-30d", "Pred mesiacom", 30, 0), ("-1y", "Pred rokom", 0, 1), ("-2y", "Pred 2 rokmi", 0, 2), ("-3y", "Pred 3 rokmi", 0, 3))


def quick_dates(cfg, base=None, lang="sk"):
    b = _pdate(base, lang) if base else _today()
    con = connect(cfg)
    try:
        out = []
        for key, label, days, years in QUICK:
            d = _shift_years(b, years) if years else b - _dt.timedelta(days=days)
            a, e = _bounds(d)
            has = con.execute("SELECT 1 FROM hourly WHERE ts>=? AND ts<? AND (n IS NULL OR n>0) LIMIT 1", (a, e)).fetchone() is not None
            out.append({"key": key, "label": label if lang == "sk" else tr("srv.stats.q_" + key.lstrip("-"), lang), "date": d.isoformat(), "has_data": has})
        return out
    finally:
        con.close()


# ---------------------------------------------------------------- export CSV
def _num(v, dec):
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(int(v))
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return ("%.*f" % (dec, v)).replace(".", ",")
    return str(v)


def _hdr(m):
    return "%s (%s)" % (m["label"], m["unit"]) if m["unit"] else m["label"]


def _zdroj(z, lang):
    if lang == "sk" or z not in ("odhad", "kalibrovany"):
        return z or ""
    return tr("srv.stats.zdroj_" + z, lang)


def _csv(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    return "﻿" + buf.getvalue()


def export_csv(cfg, kind, **p):
    lang = p.get("lang") or "sk"
    METRICS = metrics(lang)
    """CSV text (oddeľovač ;, desatinná čiarka, začína BOM). kind: day (date|d), range (date_from|from, date_to|to, group), compare (dates|d)."""
    def g(*names):
        for n in names:
            if p.get(n) not in (None, ""):
                return p[n]
        return None
    if kind == "day":
        d = g("date", "d", "day")
        day = get_day(cfg, d or _today().isoformat(), lang)
        header = [tr("srv.stats.csv_datum", lang), tr("srv.stats.csv_hodina", lang)] + [_hdr(m) for m in METRICS]
        rows = [[day["date"], e["h"]] + [_num(e.get(m["key"]), m["decimals"]) for m in METRICS] for e in day["hours"]]
        return _csv(header, rows)
    if kind == "range":
        f, t = g("date_from", "from", "from_", "od"), g("date_to", "to", "do")
        if not f or not t:
            raise ValueError(tr("srv.stats.csv_chyba_datum", lang))
        r = get_range(cfg, f, t, g("group") or "day", lang)
        header = [tr("srv.stats.csv_obdobie", lang), tr("srv.stats.csv_od", lang), tr("srv.stats.csv_do", lang)] + [_hdr(m) for m in METRICS if m["key"] != "n"] + \
                 [tr("srv.stats.csv_" + x, lang) for x in ("hdd", "kwh_hdd", "kwh_spotreba", "zdroj", "cop_kurenie", "cop_tuv", "dni", "poruchy")]
        rows = []
        for x in r["rows"] + [dict(r["totals"], key=tr("srv.stats.csv_spolu", lang), **{"from": f, "to": t})]:
            rows.append([x["key"], x["from"], x["to"]] + [_num(x.get(m["key"]), m["decimals"]) for m in METRICS if m["key"] != "n"] +
                        [_num(x.get("hdd"), 2), _num(x.get("kwh_hdd"), 2), _num(x.get("kwh_spotreba"), 2), _zdroj(x.get("kwh_zdroj"), lang),
                         _num(x.get("cop_kurenie"), 2), _num(x.get("cop_tuv"), 2), x.get("dni_s_datami", ""), x.get("poruch", "")])
        return _csv(header, rows)
    if kind == "compare":
        c = compare(cfg, g("dates", "d") or [], lang)
        header = [tr("srv.stats.csv_datum", lang), tr("srv.stats.csv_oznacenie", lang), tr("srv.stats.csv_hodina", lang)] + [_hdr(m) for m in METRICS]
        rows = []
        for dd in c["dates"]:
            for e in dd["hours"]:
                rows.append([dd["date"], dd["label"], e["h"]] + [_num(e.get(m["key"]), m["decimals"]) for m in METRICS])
        return _csv(header, rows)
    raise ValueError(tr("srv.stats.export_druh", lang))


# ---------------------------------------------------------------- profil inštalácie (profil.py)
def configure(p=None):
    """Naplní entity, stĺpce hodín, metriky a model zariadenia z profilu (pri importe z profil.json alebo predvoleného profilu).
    Volá sa pri importe; testy ju môžu zavolať s iným profilom (a potom znova configure() pre pôvodný)."""
    global TC, INV, VALVE, DEFROST, THERMOSTAT, AGENT_TEMPS, MODEL, TAB_TA, TAB_LW, TAB_PEAK, FLOWCAL_ZDROJ
    global ROOM_KEYS, HCOLS, AVG_KEYS, METRICS, METRIC_KEYS, _MDEC, _UPSERT, _SEL, _PROFILE
    p = p if p is not None else profil.load()
    _PROFILE = p
    TC = profil.esp_tc(p)
    INV = {v: k for k, v in TC.items()}
    VALVE = profil.device_entity("ventil", p)
    DEFROST = profil.device_entity("odmrazovanie", p)
    THERMOSTAT = profil.device_entity("termostat", p)
    AGENT_TEMPS = {k: ids[0] for k, ids in p["ine_teploty"].items() if ids}
    MODEL = profil.model(p)
    if MODEL:
        TAB_TA, TAB_LW, TAB_PEAK = tuple(MODEL["ta"]), tuple(MODEL["lw"]), dict(MODEL["tab"])
        FLOWCAL_ZDROJ = ("tabuľky výrobcu (AE120JXED, bez odmrazovania)" if MODEL["id"] == "AE120JXEDGH"
                         else "tabuľky výrobcu (%s, bez odmrazovania)" % (MODEL.get("nazov") or MODEL["id"]))
    else:
        TAB_TA, TAB_LW, TAB_PEAK = (), (), {}
        FLOWCAL_ZDROJ = "bez tabuľky výrobcu (prietok zo senzora alebo z konfigurácie)"
    rooms = profil.room_keys(p)
    extra = profil.extra_temp_keys(p)
    ROOM_KEYS = tuple(rooms) + tuple(extra)
    HCOLS = (("vonku", "vonku_min", "vonku_max", "ciel") + ROOM_KEYS +
             ("tw2", "tw1", "wl", "dt_beh", "tuv", "tuv_ciel", "tuv_min", "beh_min", "kurenie_min", "odmraz_min", "starty",
              "hz_avg", "hz_max", "kwh", "prikon", "kwh_dom", "kwh_cal", "q_kwh", "cop"))
    AVG_KEYS = (("vonku", "ciel") + tuple(rooms) + ("tw2", "tw1", "wl", "tuv", "tuv_ciel", "prikon"))
    METRICS = _metrics(p)
    METRIC_KEYS = [m["key"] for m in METRICS]
    _MDEC = {m["key"]: m["decimals"] for m in METRICS}
    _UPSERT = _upsert_sql()
    _SEL = "SELECT ts, n, src, %s FROM hourly" % ",".join(HCOLS)


configure()


if __name__ == "__main__":
    import sys
    c = load_cfg()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "catch_up"
    if cmd == "catch_up":
        t0 = time.time()
        print("hodín: %d, %.1f s" % (catch_up(c), time.time() - t0))
    elif cmd == "recompute":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
        H = int(time.time()) // HOUR * HOUR
        print("hodín: %d" % recompute_range(c, H - days * 86400, H + HOUR))
    elif cmd == "coverage":
        print(coverage(c))
    elif cmd == "recalibrate":
        print(json.dumps(recalibrate(c), ensure_ascii=False, indent=1))
    elif cmd == "fill_dom":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 2
        print(refresh_kwh_dom(c, days * 24))
    elif cmd == "live":
        t0 = time.time()
        r = live(c)
        print(json.dumps(r, ensure_ascii=False), "(%.0f ms)" % ((time.time() - t0) * 1000))
    else:
        print("použitie: stats.py [catch_up | recompute DNI | coverage | recalibrate | fill_dom DNI | live]")
