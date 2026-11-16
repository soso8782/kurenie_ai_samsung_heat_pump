#!/usr/bin/env python3
"""Porovnávací test A/B (SPEC_ABTEST.md): o koľko menej energie spotrebuje dom s reguláciou podľa izby (A) oproti samotnej
ekvitermickej krivke bez spätnej väzby z izby (B). Test spúšťa VÝLUČNE používateľ z webu (start()); nič sa nespúšťa samo.
Jediný zápis do ESP: select „Režim izbovej regulácie“ = „Posun krivky“ (A) alebo „Vypnutá“ (B), a to cez agent.esp_set_select().
Stav je v agent.db, tabuľka state, kľúč `abtest` (JSON). tick() volá strážca každú minútu. Iba štandardná knižnica."""
import datetime, json, math, time, traceback
import contacts, interventions, notify
import i18n
from i18n import tr
from common import REF_NAME, REF_ROOM, ROOMS, S, adb, day_bounds, get_state, hour_stats, is_dry, set_state, tcq

KEY = "abtest"
ENT_SELECT = "Režim izbovej regulácie"
OPT = {"A": "Posun krivky", "B": "Vypnutá"}
ENT_BACKUP = "Elektrický dohrev kúrenia (Backup heater)"
LENGTHS = (6, 8, 10, 14)
PATTERN = "ABBA"                 # blok sa opakuje a orezáva na dĺžku testu; prvý deň vždy A
WINDOW_FROM_H = 4                # vyhodnocuje sa 04:00 - 24:00 (prvé 4 h sú prechod)
MIN_HOURS = 16                   # deň platí, ak má aspoň toľko hodín s dátami
BAND = (23.0, 24.0)              # komfortné pásmo detskej izby
SAFE_LO, SAFE_HI = 22.0, 25.0    # poistka v dňoch B (priemer detskej za posledných 30 min)
SAFE_AVG_S = 1800
SAFE_STALE_S = 900               # dáta zberača staršie ako 15 min = poistka
START_STALE_S = 600              # štart testu vyžaduje čerstvé dáta
VENT_MANY_MIN = 120              # deň s viac ako toľkými minútami otvorených okien sa označí poznámkou „veľa vetrania“ (nevyraďuje sa)
WRITE_GAP_S = 300                # najviac 1 pokus o zápis za 5 min
MAX_FAILS = 3                    # neúspešné zápisy za deň, potom sa test zastaví
SWITCH_WINDOW_S = 300            # 00:00 - 00:05: okno prepínania, kontrola ručnej zmeny sa nerobí
MIN_DAYS_STOP = 2                # výsledok pri zastavení: aspoň 2 platné dni A a 2 platné dni B
ACTIVE = ("planned", "running")
_UNSET = object()
_cache = {"k": None, "v": None}


def _agent():
    import agent   # oneskorený import (agent importuje tento modul)
    return agent


def sk(v, nd=1):
    return interventions.sk(v, nd)


# ---------------------------------------------------------------- stav

def load(c):
    try:
        st = json.loads(get_state(c, KEY, "") or "null")
    except ValueError:
        return None
    return st if isinstance(st, dict) else None


def save(c, st):
    set_state(c, KEY, json.dumps(st, ensure_ascii=False))


def active_state(st):
    return bool(st) and st.get("status") in ACTIVE


def active(c):
    """True, ak test čaká na začiatok alebo beží (status planned / running)."""
    try:
        return active_state(load(c))
    except Exception:
        return False


def date_of(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def sec_of_day(ts):
    lt = time.localtime(ts)
    return lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec


def today_entry(st, now):
    d = date_of(now)
    for e in (st or {}).get("days") or []:
        if e.get("date") == d:
            return e
    return None


def mode_today(c, now=None):
    """'A' / 'B' podľa plánu pre dnešný deň počas aktívneho testu, inak None."""
    try:
        st = load(c)
        if not active_state(st):
            return None
        e = today_entry(st, int(now or time.time()))
        return e["mode"] if e else None
    except Exception:
        return None


def make_plan(now, n):
    """Plán od zajtrajška: A B B A A B B A ... orezané na n dní (prvý deň A)."""
    lt = time.localtime(now)
    d0 = datetime.date(lt.tm_year, lt.tm_mon, lt.tm_mday) + datetime.timedelta(days=1)
    return [{"date": (d0 + datetime.timedelta(days=i)).isoformat(), "mode": PATTERN[i % len(PATTERN)], "valid": True, "note": ""}
            for i in range(n)]


def _log_info(c, text):
    """Riadok „info“ do runs (zobrazí sa v dennej osi na webe)."""
    try:
        c.execute("INSERT INTO runs(ts,mode,status,stav,summary,actions) VALUES(?,?,?,?,?,?)",
                  (int(time.time()), get_state(c, "mode", "advise"), "info", None, text, "[]"))
        c.commit()
    except Exception:
        traceback.print_exc()


def _notify(cfg, title, msg):
    try:
        return notify.event(cfg, "abtest", title, msg)
    except Exception:
        return False, "chyba"


# ---------------------------------------------------------------- ESP (čítanie a zápis selectu)

def read_esp_mode(cfg, timeout=4):
    """Aktuálna hodnota selectu v ESP ('Posun krivky' / 'Vypnutá' / ...). Pri chybe siete vyhodí výnimku."""
    v = _agent().esp_get(cfg, "select", ENT_SELECT, timeout=timeout).get("value")
    return v if isinstance(v, str) else None


def current_mode(cfg, st):
    """Aktuálna hodnota selectu; v DRY_RUN po prvom zápise testu sa používa simulovaná (nič sa neposiela, ESP sa nezmení).
    None = ESP nedostupné."""
    if is_dry(cfg) and st and st.get("last_written"):
        return st["last_written"]
    try:
        return read_esp_mode(cfg)
    except Exception:
        return None


def write_mode(cfg, want):
    """Zapíše režim ('A'/'B') cez agent.esp_set_select a overí čítaním. True = potvrdené (v DRY_RUN vždy)."""
    opt = OPT[want]
    _agent().esp_set_select(cfg, ENT_SELECT, opt)
    if is_dry(cfg):
        return True
    time.sleep(2)
    return read_esp_mode(cfg) == opt


def _write_count(cfg, c, st, want, now):
    """Zápis s účtovaním neúspechov. Vracia (ok, text)."""
    st["last_attempt"] = OPT[want]   # ESP môže režim prijať aj keď overenie zlyhá: nesmie to vyzerať ako ručná zmena
    if want == "B":
        st["switched_by_test"] = True   # už pred zápisom: ak sa overenie nepodarí, režim A sa aj tak vráti (pravidlo 4)
    save(c, st)
    try:
        ok = write_mode(cfg, want)
        err = "" if ok else tr("srv.abtest.zapis_nepotvrdeny", i18n.home_lang(cfg))
    except Exception as e:
        ok, err = False, ("%s: %s" % (type(e).__name__, e))[:200]
    if ok:
        st["last_written"] = OPT[want]
        st["switched_by_test"] = (want == "B")
        save(c, st)
        return True, "zapísané: " + OPT[want]
    return False, err


def sync(cfg, c, st, want, now, urgent=False):
    """Zosúladí ESP s požadovaným režimom. Najviac 1 pokus za 5 min (urgent = poistka obchádza), 3 neúspechy za deň = stop."""
    opt = OPT[want]
    cur = current_mode(cfg, st)
    if cur is None:
        return "ESP nedostupné"
    if cur == opt:
        if st.get("last_written") != opt or st.get("switched_by_test") != (want == "B"):
            st["last_written"], st["switched_by_test"] = opt, (want == "B")
            save(c, st)
        return "režim už je %s" % opt
    if not urgent and now - int(st.get("last_try") or 0) < WRITE_GAP_S:
        return "čaká na ďalší pokus o zápis"
    st["last_try"] = now
    ok, txt = _write_count(cfg, c, st, want, now)
    if ok:
        return txt
    att = st.get("attempts") or {}
    n = (att.get("n", 0) if att.get("date") == date_of(now) else 0) + 1
    st["attempts"] = {"date": date_of(now), "n": n}
    save(c, st)
    if n >= MAX_FAILS and active_state(st):
        stop(cfg, c, tr("srv.abtest.stop_zapis", i18n.home_lang(cfg), n=n, err=txt), now, st=st, notify_user=True)
        return "test zastavený: zápis zlyhal"
    return "zápis zlyhal (%s), pokus %d z %d" % (txt, n, MAX_FAILS)


def restore_a(cfg, c, st, now=None):
    """Pravidlo 4: test nebeží a drží ESP v režime B -> okamžite A. Pri neúspechu sa skúsi znova pri ďalšom tiku."""
    if not st or not st.get("switched_by_test"):
        return "nie je čo vracať"
    cur = current_mode(cfg, st)
    if cur is None:
        return "ESP nedostupné, skúsim znova"
    if cur != OPT["A"]:
        try:
            ok = write_mode(cfg, "A")
        except Exception as e:
            print("abtest: návrat na režim A zlyhal: %s" % e, flush=True)
            return "zápis zlyhal, skúsim znova"
        if not ok:
            return "zápis nebol potvrdený, skúsim znova"
    st["last_written"], st["switched_by_test"] = OPT["A"], False
    save(c, st)
    return "vrátené na %s" % OPT["A"]


def startup(cfg, c):
    """Pri štarte agenta: ak test nebeží, ale drží reguláciu vypnutú, hneď ju vráť (pravidlo 4)."""
    st = load(c)
    if st and not active_state(st) and st.get("switched_by_test"):
        return restore_a(cfg, c, st)
    return "nič"


# ---------------------------------------------------------------- poistky v dňoch B

def read_conditions(cfg, now):
    """Podklady pre poistky: vek dát zberača, priemer detskej za 30 min, chybový kód, dohrev kúrenia (z ESP, None = neznámy)."""
    r = tcq(cfg, "SELECT v FROM meta WHERE k='last_sample'")
    age = now - int((r[0][0] if r else 0) or 0)
    row = tcq(cfg, "SELECT avg(value), count(*) FROM samples WHERE entity=? AND ts>=? AND ts<=?", (S[REF_ROOM], now - SAFE_AVG_S, now))[0]
    ch = tcq(cfg, "SELECT value FROM samples WHERE entity=? ORDER BY ts DESC LIMIT 1", (S.get("chyba", ""),))
    backup = None
    try:
        backup = _agent().esp_get(cfg, "binary_sensor", ENT_BACKUP, timeout=4).get("value")
    except Exception:
        pass
    return {"age": age, "detska": row[0] if row[1] else None, "chyba": ch[0][0] if ch else None, "backup": backup}


def safety(cfg, now, lang="sk"):
    """(ok, dôvod). Poistky z bodu 3 špecifikácie."""
    cd = read_conditions(cfg, now)
    n = lambda v: i18n.dec(sk(v), lang)
    if cd.get("age") is None or cd["age"] > SAFE_STALE_S:
        return False, tr("srv.abtest.s_stare", lang)
    d = cd.get("detska")
    if d is None:
        return False, tr("srv.abtest.s_chyba_data", lang, ref=REF_NAME)
    if d > SAFE_HI:
        return False, tr("srv.abtest.s_teply", lang, ref=REF_NAME, v=n(d), lim=n(SAFE_HI))
    if d < SAFE_LO:
        return False, tr("srv.abtest.s_studeny", lang, ref=REF_NAME, v=n(d), lim=n(SAFE_LO))
    ch = cd.get("chyba")
    if ch not in (None, 0, 0.0):
        return False, tr("srv.abtest.s_kod", lang, code=int(ch))
    if cd.get("backup") is True:
        return False, tr("srv.abtest.s_dohrev", lang)
    return True, ""


# ---------------------------------------------------------------- tik (každú minútu zo strážcu)

def tick(cfg, c, now=None):
    """Jeden krok testu. Vracia krátky popis (pre log/testy)."""
    now = int(now or time.time())
    lang = i18n.home_lang(cfg)
    st = load(c)
    if st is None:
        return "žiadny test"
    if not active_state(st):
        if st.get("switched_by_test"):
            return "obnova: " + restore_a(cfg, c, st, now)
        return "nečinný"
    days = st.get("days") or []
    if not days:
        return "prázdny plán"
    today = date_of(now)
    if today > days[-1]["date"]:
        return finish(cfg, c, st, now)
    if today < days[0]["date"]:
        return "čaká na začiatok (%s)" % days[0]["date"]
    if st["status"] == "planned":
        st["status"] = "running"
        save(c, st)
        _log_info(c, tr("srv.abtest.log_zacal", lang))
    entry = today_entry(st, now)
    if entry is None:
        return "dnešný deň nie je v pláne"
    cur = current_mode(cfg, st)
    lw = st.get("last_written")
    # ručná zmena selectu (mimo okna prepínania po polnoci): test sa zastaví a režim sa už neprepisuje
    if cur is not None and lw is not None and cur not in (lw, st.get("last_attempt")) and sec_of_day(now) >= SWITCH_WINDOW_S:
        stop(cfg, c, tr("srv.abtest.stop_rucne", lang), now, st=st, restore=False, notify_user=True)
        return "zastavené: režim zmenený ručne"
    want = entry["mode"] if entry["valid"] else "A"
    urgent = False
    if want == "B":
        ok, why = safety(cfg, now, lang)
        if not ok:
            entry["valid"], entry["note"] = False, tr("srv.abtest.poistka", lang, why=why)
            want, urgent = "A", True
            save(c, st)
            _log_info(c, tr("srv.abtest.log_den", lang, d=entry["date"], why=why))
            _notify(cfg, tr("srv.abtest.n_preruseny_t", lang),
                    tr("srv.abtest.n_preruseny", lang, d=entry["date"], why=why))
    if cur is not None and cur != OPT[want] and entry["valid"] and sec_of_day(now) >= WINDOW_FROM_H * 3600:
        # režim sa nestihol prepnúť do začiatku vyhodnocovacieho okna: deň by bol skreslený
        entry["valid"], entry["note"] = False, tr("srv.abtest.note_po_04", lang)
        if want == "B":
            want = "A"
        save(c, st)
    return sync(cfg, c, st, want, now, urgent)


def finish(cfg, c, st, now):
    lang = i18n.home_lang(cfg)
    st["status"] = "done"
    st["finished"] = now
    try:
        st["result"] = evaluate(cfg, st, now, lang)
    except Exception:
        traceback.print_exc()
        st["result"] = None
    save(c, st)
    res = restore_a(cfg, c, st, now)
    verdict = (st.get("result") or {}).get("verdikt") or tr("srv.abtest.vysledok_nie", lang)
    _log_info(c, tr("srv.abtest.log_skoncil", lang, v=verdict))
    _notify(cfg, tr("srv.abtest.n_skoncil_t", lang), verdict)
    return "hotovo; " + res


def stop(cfg, c, reason=None, now=None, st=None, restore=True, notify_user=False):
    """Zastaví test. restore=True -> hneď režim A; restore=False (ručná zmena) -> režim sa neprepisuje. Vracia True, ak sa zastavil."""
    now = int(now or time.time())
    lang = i18n.home_lang(cfg)
    reason = reason or tr("srv.abtest.stop_pouzivatel", lang)
    st = st if st is not None else load(c)
    if not active_state(st):
        return False
    today = date_of(now)
    for e in st.get("days") or []:
        if e["date"] >= today and e.get("valid"):
            e["valid"], e["note"] = False, tr("srv.abtest.note_zastaveny", lang)
    st["status"], st["stopped_reason"], st["stopped_ts"] = "stopped", reason, now
    if not restore:
        st["switched_by_test"] = False
    save(c, st)   # status sa uloží ešte pred zápisom: ak zápis zlyhá, pravidlo 4 ho opakuje
    if restore:
        try:
            restore_a(cfg, c, st, now)
        except Exception:
            traceback.print_exc()
    try:
        res = evaluate(cfg, st, now, lang)
        if res["A"]["n"] >= MIN_DAYS_STOP and res["B"]["n"] >= MIN_DAYS_STOP:
            st["result"] = res
            save(c, st)
    except Exception:
        traceback.print_exc()
    _log_info(c, tr("srv.abtest.log_zastaveny", lang, why=reason))
    if notify_user:
        _notify(cfg, tr("srv.abtest.n_zastaveny_t", lang),
                tr("srv.abtest.n_zastaveny", lang, why=reason, tail=tr("srv.abtest.n_zap" if restore else "srv.abtest.n_neprepisuje", lang)))
    return True


# ---------------------------------------------------------------- štart

def _fresh_age(cfg):
    r = tcq(cfg, "SELECT v FROM meta WHERE k='last_sample'")
    return int(time.time()) - int((r[0][0] if r else 0) or 0)


def when_str(ts, lang="sk"):
    if lang != "sk":
        return time.strftime("%d %b at %H:%M", time.localtime(ts)).lstrip("0")
    return time.strftime("%d.%m. o %H:%M", time.localtime(ts)).lstrip("0")


def can_start(cfg, c, now=None, esp_mode=_UNSET, lang="sk"):
    """(smie, dôvod alebo None, ts kedy sa dá spustiť alebo None)."""
    st = load(c)
    if active_state(st):
        return False, tr("srv.abtest.c_prebieha", lang), None
    if st and st.get("switched_by_test"):
        return False, tr("srv.abtest.c_vracia", lang), None
    due = c.execute("SELECT max(due_ts) FROM interventions WHERE status='caka' AND kind='ventil'").fetchone()[0]
    if due:
        return False, tr("srv.abtest.c_ventily", lang, when=when_str(due, lang)), int(due)
    age = _fresh_age(cfg)
    if age > START_STALE_S:
        return False, tr("srv.abtest.c_stare", lang, n=age // 60), None
    ch = tcq(cfg, "SELECT value FROM samples WHERE entity=? ORDER BY ts DESC LIMIT 1", (S["chyba"],))
    code = ch[0][0] if ch else None
    if code is None or code != 0:
        return False, tr("srv.abtest.c_kod", lang, code=(tr("srv.abtest.neznamy", lang) if code is None else int(code))), None
    if esp_mode is _UNSET:
        try:
            esp_mode = read_esp_mode(cfg, timeout=3)
        except Exception:
            esp_mode = None
    if esp_mode is None:
        return False, tr("srv.abtest.c_neodpoveda", lang), None
    if esp_mode != OPT["A"]:
        return False, tr("srv.abtest.c_regulacia", lang, a=OPT["A"], now=esp_mode), None
    return True, None, None


def start(cfg, c, days, now=None, lang="sk"):
    """Naplánuje test od najbližšej polnoci. Vracia (http_kód, telo)."""
    now = int(now or time.time())
    if isinstance(days, bool) or not isinstance(days, (int, float)) or days != int(days) or int(days) not in LENGTHS:
        return 400, {"error": tr("srv.abtest.dlzka", lang)}
    ok, why, _due = can_start(cfg, c, now, lang=lang)
    if not ok:
        return 400, {"error": why}
    plan = make_plan(now, int(days))
    st = {"status": "planned", "created": now, "start": plan[0]["date"], "days": plan, "switched_by_test": False,
          "stopped_reason": None, "result": None, "last_written": None, "attempts": {}}
    save(c, st)
    _log_info(c, tr("srv.abtest.log_planovany", i18n.home_lang(cfg), n=len(plan), d=plan[0]["date"]))
    return 200, {"ok": True, "state": st}


# ---------------------------------------------------------------- vyhodnotenie

def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _sd(xs):
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _r(v, nd):
    return None if v is None else round(v, nd)


def _day_raw(cfg, e, c=None):
    a, b = day_bounds(e["date"])
    hrs = hour_stats(cfg, a + WINDOW_FROM_H * 3600, b)
    ok = [h for h in hrs if h.get("n")]
    rooms = {}
    for k in ROOMS:
        xs = [h[k] for h in ok if h.get(k) is not None]
        rooms[k] = sum(xs) / len(xs) if xs and len(xs) * 2 >= len(ok) else None
    det = [h[REF_ROOM] for h in ok if h.get(REF_ROOM) is not None]   # výsledok ostáva pod kľúčom "detska" (rozhranie pre web)
    okna = None
    try:
        okna = contacts.window_minutes(c, a + WINDOW_FROM_H * 3600, b) if c is not None else None
    except Exception:
        traceback.print_exc()
    return {"b": b, "hours": len(ok), "okna_min": None if okna is None else round(okna), "kwh": sum(h.get("kwh") or 0 for h in ok), "rooms": rooms,
            "t_out": _mean([h.get("vonku") for h in ok]), "detska": _mean(det),
            "mimo": len([x for x in det if x < BAND[0] - 1e-9 or x > BAND[1] + 1e-9]),
            "tw2": _mean([h.get("tw2") for h in ok]), "starty": sum(h.get("starty") or 0 for h in ok)}


def evaluate(cfg, st, now=None, lang=None):
    """Vyhodnotenie z hour_stats (okno 04:00-24:00 každého skončeného dňa plánu). Vracia štruktúru `result` zo špecifikácie."""
    now = int(now or time.time())
    lang = lang or i18n.home_lang(cfg)
    entries = st.get("days") or []
    raws = {}
    c = None
    try:
        c = adb(cfg)
    except Exception:
        traceback.print_exc()
    for e in entries:
        if e["date"] >= date_of(now):
            continue   # deň ešte neskončil (alebo ešte nezačal)
        a, b = day_bounds(e["date"])
        if b > now:
            continue
        raws[e["date"]] = _day_raw(cfg, e, c)
    if c is not None:
        c.close()
    usable = {d: r for d, r in raws.items() if r["hours"] >= MIN_HOURS}
    # rovnaká množina izieb vo všetkých dňoch, aby sa t_in medzi dňami dalo porovnať
    common = [k for k in ROOMS if usable and all(r["rooms"].get(k) is not None for r in usable.values())]
    if not common:
        common = [k for k in ROOMS if any(r["rooms"].get(k) is not None for r in usable.values())]
    out_days = []
    for e in entries:
        d = {"date": e["date"], "mode": e["mode"], "valid": False, "note": e.get("note") or "", "hours": None, "kwh": None,
             "t_in": None, "t_out": None, "dT": None, "ua": None, "detska": None, "mimo_pasma_h": None, "tw2": None, "starty": None,
             "okna_min": None}
        r = raws.get(e["date"])
        if r is None:
            if not d["note"]:
                d["note"] = tr("srv.abtest.note_neskoncil", lang) if e["date"] >= date_of(now) else tr("srv.abtest.note_bez_dat", lang)
        else:
            d["hours"] = r["hours"]
            d["okna_min"] = r.get("okna_min")
            if d["okna_min"] is not None and d["okna_min"] > VENT_MANY_MIN:
                d["note"] = (d["note"] + "; " if d["note"] else "") + tr("srv.abtest.note_vetranie", lang, n=d["okna_min"])
            if r["hours"] < MIN_HOURS:
                d["note"] = (d["note"] + "; " if d["note"] else "") + tr("srv.abtest.note_malo", lang, h=r["hours"], min=MIN_HOURS)
            else:
                t_in = _mean([r["rooms"].get(k) for k in common])
                dT = None if t_in is None or r["t_out"] is None else t_in - r["t_out"]
                ua = r["kwh"] / (dT * r["hours"]) if dT is not None and dT > 1.0 and r["kwh"] > 0 else None
                d.update(kwh=_r(r["kwh"], 2), t_in=_r(t_in, 2), t_out=_r(r["t_out"], 2), dT=_r(dT, 2), ua=_r(ua, 5),
                         detska=_r(r["detska"], 2), mimo_pasma_h=r["mimo"], tw2=_r(r["tw2"], 1), starty=r["starty"])
                if ua is None and not d["note"]:
                    d["note"] = tr("srv.abtest.note_spotreba", lang)
                d["valid"] = bool(e.get("valid")) and ua is not None
        out_days.append(d)

    def group(m):
        ds = [d for d in out_days if d["valid"] and d["mode"] == m]
        uas = [d["ua"] for d in ds]
        g = {"n": len(ds), "ua": _r(_mean(uas), 5), "sd": _r(_sd(uas), 5)}
        for k_out, k in (("kwh", "kwh"), ("t_in", "t_in"), ("t_out", "t_out"), ("detska", "detska"), ("mimo_pasma_h", "mimo_pasma_h"), ("tw2", "tw2")):
            g[k_out] = _r(_mean([d[k] for d in ds]), 2)
        g["okna_min"] = _r(_mean([d["okna_min"] for d in ds]), 0)
        return g, uas

    gA, uA = group("A")
    gB, uB = group("B")
    uspora = neistota = None
    noise = False
    if uA and uB and gB["ua"]:
        mA, mB = sum(uA) / len(uA), sum(uB) / len(uB)
        uspora = (mB - mA) / mB * 100.0
        if len(uA) >= 2 and len(uB) >= 2:
            se = math.sqrt(_sd(uA) ** 2 / len(uA) + _sd(uB) ** 2 / len(uB))
            neistota = 2 * se / mB * 100.0
            noise = abs(uspora) < neistota
    res = {"days": out_days, "A": gA, "B": gB, "uspora_pct": _r(uspora, 1), "neistota_pct": _r(neistota, 1), "v_ramci_sumu": noise}
    res["verdikt"] = verdict(res, noise, lang)
    return res


def verdict(res, noise, lang="sk"):
    gA, gB, us, ne = res["A"], res["B"], res["uspora_pct"], res["neistota_pct"]
    sk = lambda v, nd=1, _g=globals()["sk"]: i18n.dec(_g(v, nd), lang)
    if us is None:
        return tr("srv.abtest.v_malo_dni", lang, a=gA["n"], b=gB["n"])
    parts = []
    if ne is None:
        parts.append(tr("srv.abtest.v_zatial", lang, v=sk(abs(us)), who=tr("srv.abtest.v_regulacia" if us > 0 else "srv.abtest.v_bez", lang)))
    elif noise:
        parts.append(tr("srv.abtest.v_sum", lang, us=sk(us), ne=sk(ne)))
    elif us > 0:
        parts.append(tr("srv.abtest.v_menej", lang, us=sk(us), ne=sk(ne)))
    else:
        parts.append(tr("srv.abtest.v_viac", lang, us=sk(abs(us)), ne=sk(ne)))
    if gA["mimo_pasma_h"] is not None and gB["mimo_pasma_h"] is not None:
        parts.append(tr("srv.abtest.v_pasmo", lang, ref=REF_NAME, a=sk(gA["mimo_pasma_h"]), b=sk(gB["mimo_pasma_h"]), ta=sk(gA["detska"]), tb=sk(gB["detska"])))
    if gA["tw2"] is not None and gB["tw2"] is not None:
        parts.append(tr("srv.abtest.v_voda", lang, a=sk(gA["tw2"]), b=sk(gB["tw2"])))
    warn = []
    if gA["t_out"] is not None and gB["t_out"] is not None and abs(gA["t_out"] - gB["t_out"]) > 3:
        warn.append(tr("srv.abtest.v_w_vonku", lang, v=sk(abs(gA["t_out"] - gB["t_out"]))))
    if gA.get("okna_min") is not None and gB.get("okna_min") is not None and abs(gA["okna_min"] - gB["okna_min"]) > 60:
        warn.append(tr("srv.abtest.v_w_vetranie", lang, a=sk(gA["okna_min"], 0), b=sk(gB["okna_min"], 0)))
    if min(gA["n"], gB["n"]) < 3:
        warn.append(tr("srv.abtest.v_w_malo", lang, a=gA["n"], b=gB["n"]))
    if warn:
        parts.append(tr("srv.abtest.v_upozornenie", lang, w="; ".join(warn)))
    return " ".join(parts[:4])


# ---------------------------------------------------------------- pre web a model

def evaluate_cached(cfg, st, now, lang="sk"):
    k = (json.dumps(st.get("days"), sort_keys=True), st.get("status"), int(now) // 900, lang)
    if _cache["k"] != k:
        _cache["v"] = evaluate(cfg, st, now, lang)
        _cache["k"] = k
    return _cache["v"]


def api_state(cfg, c, now=None, lang="sk"):
    """Odpoveď GET /api/abtest."""
    now = int(now or time.time())
    st = load(c)
    try:
        esp_mode = read_esp_mode(cfg, timeout=3)
    except Exception:
        esp_mode = None
    ok, why, _due = can_start(cfg, c, now, esp_mode, lang)
    today, result = None, None
    if st:
        if active_state(st):
            e = today_entry(st, now)
            today = {"date": e["date"], "mode": e["mode"]} if e else None
        if st.get("result"):
            result = st["result"]
        elif active_state(st):
            try:
                if any(e["date"] < date_of(now) for e in st.get("days") or []):
                    result = evaluate_cached(cfg, st, now, lang)
            except Exception:
                traceback.print_exc()
    state = {k: v for k, v in st.items() if k != "result"} if st else None
    return {"state": state, "today": today, "can_start": ok, "blocked_reason": why, "result": result, "esp_mode": esp_mode}


def status_brief(c, now=None):
    """Pole `abtest` v /api/status (None = žiadny test)."""
    now = int(now or time.time())
    st = load(c)
    if not st:
        return None
    days = st.get("days") or []
    today = date_of(now)
    idx, mode = 0, None
    if active_state(st):
        for i, e in enumerate(days):
            if e["date"] == today:
                idx, mode = i + 1, e["mode"]
    return {"status": st.get("status"), "today_mode": mode, "day_index": idx, "days_total": len(days), "start": st.get("start")}


def llm_text(c, now=None, lang="sk"):
    """Sekcia „POROVNÁVACÍ TEST“ pre vstup modelu (None, ak test nebeží); texty v jazyku modelu `lang`."""
    now = int(now or time.time())
    st = load(c)
    if not active_state(st):
        return None
    T = lambda key, **kw: tr("srv.prompt.ab_" + key, lang, **kw)
    days = st.get("days") or []
    e = today_entry(st, now)
    if e is None:
        head = T("planned", start=st.get("start"), n=len(days))
    else:
        idx = days.index(e) + 1
        if e["mode"] == "B" and e.get("valid"):
            mode = T("mode_b")
        elif e["mode"] == "B":
            mode = T("mode_b_off")
        else:
            mode = T("mode_a")
        head = T("running", idx=idx, n=len(days), mode=mode)
    return T("title") + "\n" + head + "\n" + T("body", ref=REF_NAME)
