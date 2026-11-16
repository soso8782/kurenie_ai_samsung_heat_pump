#!/usr/bin/env python3
"""Teplotné čidlá z Home Assistanta (SPEC_TEPLOTY.md): výber čidiel majiteľom, aliasy, skupiny (tie isté ako pri oknách),
druh čidla, vzorky každých 5 min, upozornenia na nízku/vysokú teplotu a vstup pre agenta.

Iba ČÍTA z Home Assistanta (POST /api/template s dlhodobým tokenom). Do ESP ani do HA nič nezapisuje.
Slučka `temps_loop` beží ako daemon vlákno v procese agenta (každých 60 s). Poplach `ha` pri výpadku spojenia ostáva
na `contacts` (rovnaký HA aj token) – tu sa nevytvára druhý; chyba tohto čítania sa ukazuje len na webe. Iba štandardná knižnica.
"""
import json, math, time, traceback, urllib.error, urllib.request
import ha_auth
from common import get_state, load_cfg, adb, set_state, day_bounds
import contacts
import notify
import i18n
from i18n import tr
from contacts import norm, sk, ha_conf, parse_since

TEMPLATE = (
    "{%- set ns = namespace(r=[]) -%}"
    "{%- for s in states.sensor if s.attributes.device_class == 'temperature' -%}"
    "{%- set ns.r = ns.r + [{'id': s.entity_id, 'name': s.name, 'state': s.state, 'unit': s.attributes.unit_of_measurement, "
    "'since': s.last_updated.isoformat(), 'area': area_name(s.entity_id)}] -%}"
    "{%- endfor -%}"
    "{{ ns.r | tojson }}"
)

POLL_S = 60
SAMPLE_S = 300                    # vzorka každých 5 min
SAMPLE_TOL_S = 5                  # tolerancia oneskorenia slučky
KEEP_DAYS = 90
VMIN, VMAX = -60.0, 150.0         # hodnoty mimo sú neplatné
AMIN, AMAX = -40.0, 120.0         # rozsah hraníc upozornenia
UNAVAIL_QUIET_S = 3600            # čidlo nedostupné dlhšie: žiadne upozornenie
REPEAT_S = 3 * 3600               # opakovanie upozornenia najviac raz za 3 h
RETRY_FAILED_S = 300
STALE_POLL_S = 600                # ak sa HA nečítal dlhšie, upozornenia sa nevyhodnocujú
MAX_LEARN = 12

KINDS = (("izba", "Izba (vzduch)"), ("radiator", "Hlavica radiátora"), ("vonku", "Vonku"), ("voda", "Voda / čerpadlo"),
         ("technicke", "Technické (čipy, spotrebiče)"), ("ine", "Iné"))
KIND_KEYS = tuple(k for k, _ in KINDS)
KIND_NAME = dict(KINDS)
KIND_RULES = (
    ("radiator", ("radiator",)),
    ("vonku", ("vonkaj", "outside", "irimav", "rosny", "pocitov", "index_tepla")),
    ("voda", ("m5stack", "tuv", "bojler", "tw1", "tw2", "voda", "vymennik", "vytlak", "delta")),
    ("technicke", ("slzb", "chip", "cip", "switch", "skoda", "octavia", "rura", "oven", "probe", "cpu", "nspanel", "kontakt",
                   "okno_kontakt", "dvere_kontakt")),
)
FORCE_OTHER = ("kotoln", "technick")     # navyše k contacts.FORCE_OTHER
_UNSET = object()


# ---------------------------------------------------------------- predvolené zaradenie

def kind_name(kind, lang="sk"):
    """Názov druhu čidla v jazyku `lang` (sk = KIND_NAME)."""
    if lang == "sk" or kind not in KIND_NAME:
        return KIND_NAME.get(kind, kind)
    return tr("srv.temps.kind_" + kind, lang)


def default_kind(eid, name, in_room_group):
    blob = norm(eid) + " " + norm(name)
    for kind, kws in KIND_RULES:
        if any(k in blob for k in kws):
            return kind
    return "izba" if in_room_group else "ine"


def guess_group(c, eid, name, area):
    blob = norm(" ".join((eid or "", name or "", area or "")))
    if any(k in blob for k in FORCE_OTHER):
        return contacts.other_id(c)
    return contacts.guess_group(c, eid, name, area)


def default_enabled(kind, gid, value, oid):
    return 1 if (kind == "izba" and gid != oid and value is not None) else 0


# ---------------------------------------------------------------- Home Assistant (čítanie)

def to_celsius(state, unit):
    """Text stavu + jednotka -> °C (float) alebo None, ak nie je číslo / je mimo −60…150 °C. °F sa prepočíta."""
    try:
        v = float(str(state).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    u = str(unit or "").strip().replace("º", "°").upper()
    if u in ("°F", "F"):
        v = (v - 32.0) * 5.0 / 9.0
    if not VMIN <= v <= VMAX:
        return None
    return round(v, 2)


def fetch(cfg, timeout=10, force=False, lang=None):
    """Zoznam teplotných čidiel z HA: [{"id","name","state","unit","since","area"}]. Pri chybe RuntimeError so slovenským textom
    (token sa v texte nikdy nevyskytuje)."""
    lang = lang or i18n.home_lang(cfg)
    url, token = ha_conf(cfg)
    if not url or not token:
        raise RuntimeError(tr("srv.contacts.ha_nenastaveny", lang))
    ha_auth.check(url, token, force, lang)
    req = urllib.request.Request(url + "/api/template", data=json.dumps({"template": TEMPLATE}).encode("utf-8"), method="POST",
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(4_000_000).decode("utf-8", "replace")
        ha_auth.ok(url, token)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            ha_auth.failed(url, token)
            raise RuntimeError(ha_auth.message(e.code, lang))
        if e.code == 404:
            raise RuntimeError(tr("srv.contacts.ha_404", lang))
        raise RuntimeError(tr("srv.contacts.ha_http", lang, code=e.code))
    except urllib.error.URLError as e:
        raise RuntimeError(tr("srv.contacts.ha_spojenie", lang, err=str(getattr(e, "reason", e))[:150]))
    except Exception as e:
        raise RuntimeError(tr("srv.contacts.ha_spojenie", lang, err=(type(e).__name__ + " " + str(e))[:150]))
    try:
        data = json.loads(raw.strip())
    except ValueError:
        raise RuntimeError(tr("srv.temps.ha_json", lang))
    if not isinstance(data, list):
        raise RuntimeError(tr("srv.contacts.ha_tvar", lang))
    return [d for d in data if isinstance(d, dict) and isinstance(d.get("id"), str) and d["id"].startswith("sensor.")]


def _int_state(c, key, default, lo, hi):
    try:
        v = int(float(get_state(c, key, default)))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def notify_min(c):
    return _int_state(c, "temp_notify_min", 15, 1, 240)


def apply_items(c, items, now=None):
    """Zapíše stav čidiel z HA do `temp_sensors`. Čidlo, ktoré v zozname chýba, ostane s hodnotou „nedostupné“.
    Vracia počet nových čidiel."""
    now = int(now or time.time())
    contacts.ensure(c)
    oid = contacts.other_id(c)
    seen, new = set(), 0
    for d in items:
        eid = d["id"]
        seen.add(eid)
        name = str(d.get("name") or eid)[:200]
        val = to_celsius(d.get("state"), d.get("unit"))
        upd = parse_since(d.get("since")) or now
        area = d.get("area")
        area = str(area)[:100] if area else None
        row = c.execute("SELECT group_id FROM temp_sensors WHERE entity_id=?", (eid,)).fetchone()
        if row is None:
            gid = guess_group(c, eid, name, area)
            kind = default_kind(eid, name, gid != oid)
            c.execute("INSERT INTO temp_sensors(entity_id, ha_name, alias, group_id, enabled, kind, value, updated, area, note, alert_min, alert_max, "
                      "first_seen, last_seen) VALUES(?,?,NULL,?,?,?,?,?,?,NULL,NULL,NULL,?,?)",
                      (eid, name, gid, default_enabled(kind, gid, val, oid), kind, val, upd, area, now, now))
            new += 1
        else:
            gid = row[0]
            if gid is None or not contacts.group_by_id(c, gid):
                gid = oid
            c.execute("UPDATE temp_sensors SET ha_name=?, value=?, updated=?, area=?, group_id=?, last_seen=? WHERE entity_id=?",
                      (name, val, upd, area, gid, now, eid))
    for (eid,) in c.execute("SELECT entity_id FROM temp_sensors WHERE value IS NOT NULL").fetchall():
        if eid not in seen:
            c.execute("UPDATE temp_sensors SET value=NULL, updated=? WHERE entity_id=?", (now, eid))
    c.commit()
    return new


def _set_poll_state(c, ok, error=None, now=None, count=None):
    now = int(now or time.time())
    if ok:
        if count is not None:
            set_state(c, "temp_ha_count", count)
        set_state(c, "temp_ha_fails", 0)
        set_state(c, "temp_ha_last_ok", now)
        set_state(c, "temp_ha_error", "")
    else:
        try:
            n = int(get_state(c, "temp_ha_fails", "0") or 0)
        except ValueError:
            n = 0
        set_state(c, "temp_ha_fails", n + 1)
        set_state(c, "temp_ha_error", str(error or "")[:300])


def sample(c, now=None):
    """Vzorka zapnutých čidiel s platnou hodnotou (najviac raz za 5 min) a raz denne zmazanie vzoriek starších ako 90 dní.
    Vracia počet zapísaných vzoriek."""
    now = int(now or time.time())
    n = 0
    try:
        last = int(float(get_state(c, "temp_sample_ts", "0") or 0))
    except ValueError:
        last = 0
    if now - last >= SAMPLE_S - SAMPLE_TOL_S or last > now:
        rows = c.execute("SELECT entity_id, value FROM temp_sensors WHERE enabled=1 AND value IS NOT NULL").fetchall()
        c.executemany("INSERT INTO temp_samples(ts, entity_id, value) VALUES(?,?,?)", [(now, e, v) for e, v in rows])
        c.commit()
        set_state(c, "temp_sample_ts", now)
        n = len(rows)
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    if get_state(c, "temp_clean_day", "") != day:
        c.execute("DELETE FROM temp_samples WHERE ts<?", (now - KEEP_DAYS * 86400,))
        c.commit()
        set_state(c, "temp_clean_day", day)
    return n


def poll(cfg, c, now=None):
    """Jedno čítanie z HA + vzorka. Vracia {"ok","count","error"}; stav čítania uloží do `state` (kľúče temp_ha_*)."""
    now = int(now or time.time())
    url, token = ha_conf(cfg)
    if not url or not token:
        return {"ok": False, "count": 0, "error": "nenastavené"}
    try:
        items = fetch(cfg)
        apply_items(c, items, now)
        _set_poll_state(c, True, now=now, count=len(items))
        sample(c, now)
        return {"ok": True, "count": len(items), "error": None}
    except Exception as e:
        err = str(e) if isinstance(e, RuntimeError) else "%s: %s" % (type(e).__name__, str(e)[:200])
        try:
            _set_poll_state(c, False, err, now)
        except Exception:
            traceback.print_exc()
        return {"ok": False, "count": 0, "error": err}


def status(cfg, c, now=None, lang="sk"):
    """Stav spojenia pre panel čidiel: ako contacts.ha_status, ale počet a chyba sa týkajú teplotného čítania."""
    now = int(now or time.time())
    h = dict(contacts.ha_status(cfg, c, lang))
    if not h["configured"]:
        return h
    try:
        fails = int(get_state(c, "temp_ha_fails", "0") or 0)
    except ValueError:
        fails = 0
    try:
        last = int(float(get_state(c, "temp_ha_last_ok", "") or 0)) or None
    except ValueError:
        last = None
    try:
        h["count"] = int(get_state(c, "temp_ha_count", "")) if last else None
    except ValueError:
        h["count"] = None
    h["last_ok"] = last
    if fails:
        h["ok"], h["error"] = False, get_state(c, "temp_ha_error", "") or tr("srv.temps.nepodarilo_precitat", lang)
    elif not last:
        h["ok"], h["error"] = False, tr("srv.temps.cakam", lang)
    elif now - last >= 180:
        h["ok"], h["error"] = False, tr("srv.temps.stare", lang)
    return h


# ---------------------------------------------------------------- zoznamy a úpravy

SCOLS = ("entity_id", "ha_name", "alias", "group_id", "enabled", "kind", "value", "updated", "area", "note", "alert_min", "alert_max")


def label(c, entity_id):
    r = c.execute("SELECT alias, ha_name FROM temp_sensors WHERE entity_id=?", (entity_id,)).fetchone()
    if not r:
        return entity_id
    return (r[0] or "").strip() or r[1] or entity_id


def stats(c, a, b):
    """{entity_id: (priemer, min, max, počet)} zo vzoriek v [a, b)."""
    return {e: (av, mn, mx, n) for e, av, mn, mx, n in c.execute(
        "SELECT entity_id, avg(value), min(value), max(value), count(*) FROM temp_samples WHERE ts>=? AND ts<? GROUP BY entity_id",
        (int(a), int(b)))}


def change_3h(c, eid, value, now):
    """Zmena hodnoty oproti vzorke spred ~3 h (±20 min); None ak taká vzorka nie je."""
    if value is None:
        return None
    t = int(now) - 3 * 3600
    r = c.execute("SELECT value FROM temp_samples WHERE entity_id=? AND ts>=? AND ts<=? ORDER BY abs(ts-?) LIMIT 1",
                  (eid, t - 1200, t + 1200, t)).fetchone()
    return None if r is None else round(value - r[0], 2)


def _r(v, nd=2):
    return None if v is None else round(v, nd)


def list_sensors(c, now=None):
    """Všetky čidlá ako slovníky (s `label`, `available`, `avg24`/`min24`/`max24`)."""
    now = int(now or time.time())
    contacts.ensure(c)
    st = stats(c, now - 86400, now + 1)
    out = []
    for r in c.execute("SELECT %s FROM temp_sensors ORDER BY lower(coalesce(nullif(alias,''), ha_name)), entity_id" % ", ".join(SCOLS)):
        d = dict(zip(SCOLS, r))
        d["enabled"] = bool(d["enabled"])
        d["label"] = (d["alias"] or "").strip() or d["ha_name"] or d["entity_id"]
        d["available"] = d["value"] is not None
        s = st.get(d["entity_id"])
        d["avg24"], d["min24"], d["max24"] = (_r(s[0]), _r(s[1]), _r(s[2])) if s and d["enabled"] else (None, None, None)
        out.append(d)
    return out


def tree(c, now=None):
    """Skupiny (tie isté ako pri oknách) s čidlami; čidlá bez platnej skupiny idú do „Ostatné“."""
    gs = contacts.groups(c)
    oid = contacts.other_id(c)
    byg = {g["id"]: dict(g, sensors=[]) for g in gs}
    for d in list_sensors(c, now):
        byg[d["group_id"] if d["group_id"] in byg else oid]["sensors"].append(d)
    return [byg[g["id"]] for g in gs]


def api_state(cfg, c, now=None, lang="sk"):
    now = int(now or time.time())
    return {"notify_min": notify_min(c), "ha": status(cfg, c, now, lang), "kinds": [{"key": k, "name": n if lang == "sk" else tr("srv.temps.kind_" + k, lang)} for k, n in KINDS],
            "groups": [dict(g, name=contacts.group_label(g["name"], lang)) for g in tree(c, now)], "other_group_id": contacts.other_id(c)}


def move_group(c, from_gid, to_gid):
    """Po zmazaní skupiny presunie jej čidlá (volá app.py)."""
    c.execute("UPDATE temp_sensors SET group_id=? WHERE group_id=?", (to_gid, from_gid))
    c.commit()


def _threshold(v, what, lang="sk"):
    """None / "" = bez hranice; inak číslo v rozsahu −40…120."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ValueError(tr("srv.temps.cislo", lang, what=what))
    try:
        x = float(v.strip().replace(",", ".")) if isinstance(v, str) else float(v)
    except ValueError:
        raise ValueError(tr("srv.temps.cislo", lang, what=what))
    if not math.isfinite(x):
        raise ValueError(tr("srv.temps.cislo", lang, what=what))
    if not AMIN <= x <= AMAX:
        raise ValueError(tr("srv.temps.rozsah", lang, what=what, lo=AMIN, hi=AMAX))
    return round(x, 2)


def update_sensor(c, entity_id, alias=_UNSET, group_id=_UNSET, enabled=_UNSET, kind=_UNSET, note=_UNSET,
                  alert_min=_UNSET, alert_max=_UNSET, lang="sk"):
    """Úprava čidla. Neplatný vstup = ValueError so slovenským textom."""
    contacts.ensure(c)
    cur = c.execute("SELECT alert_min, alert_max FROM temp_sensors WHERE entity_id=?", (entity_id,)).fetchone()
    if not cur:
        raise ValueError(tr("srv.temps.neznane_cidlo", lang))
    sets, args = [], []
    if alias is not _UNSET:
        if alias is not None and not isinstance(alias, str):
            raise ValueError(tr("srv.temps.meno_text", lang))
        a = (alias or "").strip()
        if len(a) > 60:
            raise ValueError(tr("srv.temps.meno_60", lang))
        sets.append("alias=?"); args.append(a or None)
    if group_id is not _UNSET:
        if group_id is None:
            group_id = contacts.other_id(c)
        if isinstance(group_id, bool) or not isinstance(group_id, int) or not contacts.group_by_id(c, group_id):
            raise ValueError(tr("srv.temps.neplatna_skupina", lang))
        sets.append("group_id=?"); args.append(group_id)
    if enabled is not _UNSET:
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() in ("1", "true", "yes", "on")
        elif not isinstance(enabled, (bool, int)):
            raise ValueError(tr("srv.temps.neplatne_zapnutie", lang))
        sets.append("enabled=?"); args.append(1 if enabled else 0)
    if kind is not _UNSET:
        if kind not in KIND_KEYS:
            raise ValueError(tr("srv.temps.neplatny_druh", lang))
        sets.append("kind=?"); args.append(kind)
    if note is not _UNSET:
        if note is not None and not isinstance(note, str):
            raise ValueError(tr("srv.temps.poznamka_text", lang))
        n = (note or "").strip()
        if len(n) > 200:
            raise ValueError(tr("srv.temps.poznamka_200", lang))
        sets.append("note=?"); args.append(n or None)
    lo, hi = cur
    if alert_min is not _UNSET:
        lo = _threshold(alert_min, tr("srv.temps.dolna", lang), lang)
        sets.append("alert_min=?"); args.append(lo)
    if alert_max is not _UNSET:
        hi = _threshold(alert_max, tr("srv.temps.horna", lang), lang)
        sets.append("alert_max=?"); args.append(hi)
    if lo is not None and hi is not None and lo >= hi:
        raise ValueError(tr("srv.temps.dolna_horna", lang))
    if not sets:
        raise ValueError(tr("srv.temps.nic_na_ulozenie", lang))
    c.execute("UPDATE temp_sensors SET %s WHERE entity_id=?" % ", ".join(sets), (*args, entity_id))
    c.commit()


def save_settings(c, body, lang="sk"):
    """Čas upozornenia (min). Vracia text zmeny; neplatný vstup = ValueError."""
    v = body.get("notify_min")
    if v is None:
        raise ValueError(tr("srv.temps.nic_na_ulozenie", lang))
    if isinstance(v, bool) or (isinstance(v, float) and v != int(v)):
        raise ValueError(tr("srv.temps.cas_cele", lang))
    try:
        n = int(str(v).strip()) if isinstance(v, str) else int(v)
    except (TypeError, ValueError):
        raise ValueError(tr("srv.temps.cas_cele", lang))
    if not 1 <= n <= 240:
        raise ValueError(tr("srv.temps.cas_rozsah", lang))
    contacts.ensure(c)
    set_state(c, "temp_notify_min", n)
    return tr("srv.temps.log_cas", lang, n=n)


# ---------------------------------------------------------------- vstup pre agenta a denné štatistiky

def _enabled_rows(c, now):
    return [d for d in list_sensors(c, now) if d["enabled"]]


def llm_text(cfg, c, now=None):
    """Sekcia „TEPLOTNÉ ČIDLÁ Z HOME ASSISTANTU“ pre vstup modelu; None, ak nie je zapnuté žiadne čidlo."""
    now = int(now or time.time())
    en = _enabled_rows(c, now)
    if not en:
        return None
    ml = i18n.home_lang(cfg)
    T = lambda key, **kw: tr("srv.prompt.ts_" + key, ml, **kw)
    N = lambda v: i18n.dec(sk(v), ml)
    gmap = {g["id"]: g for g in contacts.groups(c)}
    oid = contacts.other_id(c)
    lines = [T("head"), T("intro")]
    by = {}
    for d in en:
        by.setdefault(d["group_id"] if d["group_id"] in gmap else oid, []).append(d)
    for gid in sorted(by, key=lambda g: (gmap[g]["sort"], g)):
        lines.append("%s:" % contacts.group_label(gmap[gid]["name"], ml))
        for d in by[gid][:60]:
            kind = kind_name(d["kind"], ml).split(" (")[0].lower()
            if d["available"]:
                txt = T("now", v=N(d["value"]))
            else:
                txt = T("unavail", t=time.strftime("%d.%m. %H:%M", time.localtime(d["updated"] or now)))
            if d["avg24"] is not None:
                txt += T("avg24", a=N(d["avg24"]), mn=N(d["min24"]), mx=N(d["max24"]))
            ch = change_3h(c, d["entity_id"], d["value"], now)
            if ch is not None:
                txt += T("ch3", v=("+" if ch > 0 else "") + N(ch))
            if d["note"]:
                txt += T("note", v=d["note"])
            lines.append("- %s [%s]: %s" % (d["label"], kind, txt))
    if not status(cfg, c, now)["ok"]:
        lines.append(T("ha_down"))
    return "\n".join(lines)


def day_temps(c, a, b):
    """Pre /api/day: [{"label","group","kind","avg","min","max"}] zapnutých čidiel so vzorkami v [a, b)."""
    st = stats(c, a, b)
    gmap = {g["id"]: g["name"] for g in contacts.groups(c)}
    oid = contacts.other_id(c)
    out = []
    for d in list_sensors(c, int(b) - 1):
        s = st.get(d["entity_id"])
        if d["enabled"] and s:
            out.append({"label": d["label"], "group": gmap.get(d["group_id"]) or gmap.get(oid), "kind": d["kind"],
                        "avg": _r(s[0]), "min": _r(s[1]), "max": _r(s[2])})
    return out


def daily_text(c, days, now=None, today=True, lang="sk"):
    """Pre učenie a týždenný súhrn: tabuľka „deň | čidlo… “ s Ø (min–max) zapnutých čidiel (najviac 12). None, ak nie sú dáta."""
    now = int(now or time.time())
    en = _enabled_rows(c, now)
    if not en:
        return None
    a0, _ = day_bounds(time.strftime("%Y-%m-%d", time.localtime(now - (days - 1) * 86400)))
    allst = {}
    rows = c.execute("SELECT entity_id, ts, value FROM temp_samples WHERE ts>=? ORDER BY ts", (a0,)).fetchall()
    have = {r[0] for r in rows}
    en = [d for d in en if d["entity_id"] in have][:MAX_LEARN]
    if not en:
        return None
    ids = {d["entity_id"] for d in en}
    for e, ts, v in rows:
        if e in ids:
            allst.setdefault((time.strftime("%Y-%m-%d", time.localtime(ts)), e), []).append(v)
    dates = []
    t = a0
    while t <= now:
        dates.append(time.strftime("%Y-%m-%d", time.localtime(t)))
        t += 86400
    dates = [d for d in dict.fromkeys(dates)]
    if not today:
        dates = [d for d in dates if d != time.strftime("%Y-%m-%d", time.localtime(now))]
    gmap = {g["id"]: g["name"] for g in contacts.groups(c)}
    head = tr("srv.prompt.ts_day", lang) + " | " + " | ".join("%s (%s, %s)" % (d["label"], contacts.group_label(gmap.get(d["group_id"], "?"), lang),
                                                                      kind_name(d["kind"], lang).split(" (")[0].lower()) for d in en)
    out = [head]
    for day in dates:
        cells = []
        for d in en:
            v = allst.get((day, d["entity_id"]))
            cells.append("-" if not v else "%s (%s–%s)" % (i18n.dec(sk(sum(v) / len(v)), lang), i18n.dec(sk(min(v)), lang), i18n.dec(sk(max(v)), lang)))
        out.append(day + " | " + " | ".join(cells))
    return "\n".join(out)


# ---------------------------------------------------------------- upozornenia

def _load_mem(c):
    try:
        m = json.loads(get_state(c, "temp_notified", "") or "{}")
        return m if isinstance(m, dict) else {}
    except ValueError:
        return {}


def _send(cfg, title, msg):
    try:
        ok, err = notify.event(cfg, "teplota", title, msg)
    except Exception:
        return False
    return bool(ok) or err == "nenastavené"   # bez webhooku niet čo opakovať


def check_notifications(cfg, c, now=None):
    """Upozornenia na nízku/vysokú teplotu zapnutých čidiel s hranicou (SPEC_TEPLOTY.md). Pamäť (state.temp_notified)
    prežije reštart: {entity_id: {dir, t0, sent, ok, n}}. Vracia počet odoslaných upozornení."""
    now = int(now or time.time())
    try:
        last = int(float(get_state(c, "temp_ha_last_ok", "0") or 0))
    except ValueError:
        last = 0
    if not last or now - last > STALE_POLL_S:
        return 0                                     # bez čerstvých dát sa nič nevyhodnocuje
    hl = i18n.home_lang(cfg)
    mem = _load_mem(c)
    thr = notify_min(c)
    gname = {g["id"]: g["name"] for g in contacts.groups(c)}
    oid = contacts.other_id(c)
    rows = {r[0]: r for r in c.execute("SELECT entity_id, alias, ha_name, group_id, value, updated, alert_min, alert_max FROM temp_sensors WHERE enabled=1")}
    sent, changed = 0, False
    for eid in list(mem):                            # vypnuté / zmazané / bez hranice: pamäť sa potichu zahodí
        r = rows.get(eid)
        if r is None or (r[6] is None and r[7] is None):
            del mem[eid]
            changed = True
    for eid, (_e, alias, hn, gid, val, upd, lo, hi) in rows.items():
        if lo is None and hi is None:
            continue
        lab = (alias or "").strip() or hn or eid
        gn = contacts.group_label(gname.get(gid) or gname.get(oid), hl)
        m = mem.get(eid)
        if val is None:                              # nedostupné: krátko počkáme, dlhšie potichu zabudneme
            if m and now - (upd or now) > UNAVAIL_QUIET_S:
                del mem[eid]
                changed = True
            continue
        d = "low" if (lo is not None and val < lo) else ("high" if (hi is not None and val > hi) else None)
        if d is None:
            if m:
                if m.get("sent"):
                    _send(cfg, tr("srv.temps.v_norme_t", hl), tr("srv.temps.v_norme", hl, g=gn, lab=lab, v=i18n.dec(sk(val), hl)))
                    sent += 1
                del mem[eid]
                changed = True
            continue
        if not m or m.get("dir") != d:
            m = mem[eid] = {"dir": d, "t0": now, "sent": 0, "ok": False, "n": 0}
            changed = True
        mins = max(0, (now - m["t0"]) // 60)
        if mins < thr:
            continue
        limit = lo if d == "low" else hi
        if not m["sent"] or (not m.get("ok") and now - m["sent"] >= RETRY_FAILED_S) or (m.get("ok") and now - m["sent"] >= REPEAT_S):
            ok = _send(cfg, tr("srv.temps." + d + "_t", hl), tr("srv.temps." + d, hl, g=gn, lab=lab, v=i18n.dec(sk(val), hl), lim=i18n.dec(sk(limit), hl), n=mins))
            m.update(sent=now, ok=bool(ok), n=m["n"] + 1)
            changed, sent = True, sent + 1
    if changed:
        set_state(c, "temp_notified", json.dumps(mem))
    return sent


# ---------------------------------------------------------------- slučka

def temps_loop():
    """Daemon vlákno procesu agenta: každých 60 s prečíta teploty z HA, uloží vzorku a vybaví upozornenia. Nikdy nespadne."""
    time.sleep(15)
    while True:
        try:
            cfg = load_cfg()
            if ha_conf(cfg)[1]:
                c = adb(cfg)
                try:
                    poll(cfg, c)
                    check_notifications(cfg, c)
                finally:
                    c.close()
        except Exception:
            try:
                traceback.print_exc()
            except Exception:
                pass
        time.sleep(POLL_S)
