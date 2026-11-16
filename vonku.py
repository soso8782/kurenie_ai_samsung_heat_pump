#!/usr/bin/env python3
"""Teploty podľa vonkajšej teploty (SPEC_vonku.md): GET /api/vonku. Pre pásma vonkajšej teploty min/priemer/max v izbách a vo vode.
Zdroj: stats.db, tabuľka hourly (hodinové priemery); otvára sa len na čítanie. Iba štandardná knižnica."""
import bisect, datetime as _dt, math, sqlite3, time

import profil
import stats
from i18n import tr

KROKY = (1, 2, 3, 5)
MIN_HODIN = 3
P = profil.load()
# izby z profilu (referenčná prvá) a ďalšie sledované teploty (chodba, kotolňa, …); názvy z profilu
IZBY = list(profil.room_keys(P)) + [k for k in profil.extra_temp_keys(P) if k != "kotolna"]   # kotolňa nie je obytná izba
VODA = ["tw2", "tw1", "wl"]
NAZVY = dict(profil.room_names(P), chodba="Chodba", kotolna="Kotolňa", tw2="Voda výstup", tw1="Spiatočka", wl="Cieľ krivky")
for _k in IZBY:
    NAZVY.setdefault(_k, _k.replace("_", " ").capitalize())


def _nazov(c, lang="sk"):
    if c in ("chodba", "kotolna", "tw2", "tw1", "wl") and lang != "sk":
        return tr("srv.vonku.n_" + c, lang)
    return NAZVY[c]


def _hours(cfg, a, b):
    """Zoznam (ts, vonku, {stĺpec: hodnota}, kwh, beh_min) pre [a, b) kde vonku nie je NULL; pri chýbajúcej DB/stĺpcoch prázdny."""
    try:
        con = stats._ro(stats._db_path(cfg), stats._timeout(cfg))
    except Exception:
        return []
    try:
        have = {r[1] for r in con.execute("PRAGMA table_info(hourly)")}
        if "vonku" not in have or "ts" not in have:
            return []
        sel = lambda c: c if c in have else "NULL"
        cols = IZBY + VODA
        if "kwh_cal" in have or "kwh" in have:
            pump = "COALESCE(%s, %s)" % (sel("kwh_cal"), sel("kwh"))
        else:
            pump = "NULL"
        q = "SELECT ts, vonku, %s, %s, %s FROM hourly WHERE vonku IS NOT NULL AND ts>=? AND ts<? ORDER BY ts" % (
            ", ".join(sel(c) for c in cols), pump, sel("beh_min"))
        rows = con.execute(q, (a, b)).fetchall()
    except Exception:
        return []
    finally:
        con.close()
    n = len(IZBY) + len(VODA)
    return [(r[0], r[1], dict(zip(cols, r[2:2 + n])), r[2 + n], r[3 + n]) for r in rows]


def _r1(v):
    return round(v + 0.0, 1)


def _stat(vals):
    """vals = [(ts, hodnota)] (vzostupne podľa ts) -> {min, avg, max, n, min_ts, max_ts} alebo None pri n < MIN_HODIN."""
    if len(vals) < MIN_HODIN:
        return None
    mn = mx = vals[0]
    s = 0.0
    for t, v in vals:
        s += v
        if v <= mn[1]:
            mn = (t, v)
        if v >= mx[1]:
            mx = (t, v)
    return {"min": _r1(mn[1]), "avg": _r1(s / len(vals)), "max": _r1(mx[1]), "n": len(vals),
            "min_ts": mn[0], "max_ts": mx[0]}


def report(cfg, od=None, do=None, krok=2, now=None, lang="sk"):
    """Pásma vonkajšej teploty (krok °C) s min/avg/max izieb a vody. od/do = 'YYYY-MM-DD' vrátane; None = od najstaršieho záznamu
    po dnes. Neplatný vstup vyhodí ValueError so slovenským textom."""
    try:
        krok = int(krok)
    except (TypeError, ValueError):
        raise ValueError(tr("srv.vonku.krok_1235", lang))
    if krok not in KROKY:
        raise ValueError(tr("srv.vonku.krok_1235", lang))
    now = int(now or time.time())
    today = _dt.date.fromtimestamp(now)
    d0 = stats._pdate(od, lang) if od else None
    d1 = stats._pdate(do, lang) if do else today
    if d0 is not None and d1 < d0:
        raise ValueError(tr("srv.vonku.koniec_pred", lang))
    if d0 is None:
        try:
            con = stats._ro(stats._db_path(cfg), stats._timeout(cfg))
            try:
                m = con.execute("SELECT MIN(ts) FROM hourly WHERE vonku IS NOT NULL").fetchone()[0]
            finally:
                con.close()
        except Exception:
            m = None
        d0 = _dt.date.fromtimestamp(m) if m is not None else d1
        if d0 > d1:
            d0 = d1
    a = stats._midnight(d0)
    b = stats._midnight(d1 + _dt.timedelta(days=1))
    hrs = _hours(cfg, a, b)

    bands = {}
    for h in hrs:
        bands.setdefault(int(math.floor(h[1] / krok)) * krok, []).append(h)

    pasma, used = [], set()
    for lo in sorted(bands):
        hh = bands[lo]
        if len(hh) < MIN_HODIN:
            continue
        p = {"od_c": lo, "do_c": lo + krok, "hodin": len(hh), "vonku_avg": _r1(sum(h[1] for h in hh) / len(hh))}
        for c in IZBY + VODA:
            st = _stat([(h[0], h[2][c]) for h in hh if h[2][c] is not None])
            p[c] = st
            if st:
                used.add(c)
        kw = [h[3] for h in hh if h[3] is not None]
        p["kwh_h"] = round(sum(kw) / len(kw), 2) if len(kw) >= MIN_HODIN else None
        bm = [h[4] for h in hh if h[4] is not None]
        p["beh_pct"] = int(round(sum(bm) / len(bm) / 60.0 * 100)) if len(bm) >= MIN_HODIN else None
        pasma.append(p)

    stlpce = [{"key": c, "name": _nazov(c, lang), "skupina": "izba"} for c in IZBY if c in used]
    stlpce += [{"key": c, "name": _nazov(c, lang), "skupina": "voda"} for c in VODA if c in used]
    return {"od": d0.isoformat(), "do": d1.isoformat(), "krok": krok, "pasma": pasma, "stlpce": stlpce,
            "hodin_spolu": sum(p["hodin"] for p in pasma),
            "prvy_den": stats._dstr(hrs[0][0]) if hrs else None,
            "posledny_den": stats._dstr(hrs[-1][0]) if hrs else None}


# ---------------------------------------------------------------- SPEC_akokuri: na akú teplotu vody kúri čerpadlo pri akej vonkajšej teplote
# entity zariadenia a nastavení krivky z profilu (chýbajúca entita = prázdny názov = žiadne dáta, nie pád)
_dev = lambda k: profil.device_entity(k, P)
_set = lambda k: profil.setting_entity(k, P)
E_VONKU, E_TW2, E_TW1 = _dev("vonku"), _dev("tw2"), _dev("tw1")
E_WL, E_FREQ, E_POSUN = _dev("wl"), _dev("freq"), _dev("posun")
# (kľúč v odpovedi API, entita); „max_posun_kladny“ = profilové nastavenie max_posun, „max_posun“ = max_posun_zaporny (názvy z v1)
NAST = [("fsv2011", _set("fsv2011")), ("fsv2012", _set("fsv2012")),
        ("fsv2021", _set("fsv2021")), ("fsv2022", _set("fsv2022")),
        ("fsv2031", _set("fsv2031")), ("fsv2032", _set("fsv2032")),
        ("fsv4013", _set("fsv4013")), ("posun", E_POSUN),
        ("max_posun_kladny", _set("max_posun")), ("max_posun", _set("max_posun_zaporny")),
        ("zosilnenie", _set("zosilnenie"))]
MIN_KURENIE = 30      # minút kúrenia v pásme
ROZBEH = 10           # minút nepretržitého behu pred započítaním (aj po odmrazovaní)
MAX_DNI = 400
MAX_ZMIEN = 50


def _pct(srt, p):
    """Percentil (lineárna interpolácia) z vzostupne zoradeného zoznamu."""
    k = (len(srt) - 1) * p / 100.0
    i = int(math.floor(k))
    j = min(i + 1, len(srt) - 1)
    return srt[i] + (srt[j] - srt[i]) * (k - i)


def _krivka(x, s):
    """Voda podľa nastavenej krivky (bez posunu) pri vonkajšej teplote x; None pri chýbajúcich nastaveniach."""
    try:
        x1, x2, y1, y2 = s["fsv2011"], s["fsv2012"], s["fsv2031"], s["fsv2032"]
    except KeyError:
        return None
    if None in (x1, x2, y1, y2):
        return None
    if x2 == x1:
        return _r1(y1)
    if x <= x1:
        return _r1(y1)
    if x >= x2:
        return _r1(y2)
    return _r1(y1 + (y2 - y1) * (x - x1) / (x2 - x1))


def _blizky(pr, x):
    """ts hodnoty z vzostupne zoradeného [(hodnota, ts)] najbližšej k x (pri zhode najstaršie ts)."""
    i = bisect.bisect_left(pr, (x,))
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(pr):
            v = pr[j][0]
            j = bisect.bisect_left(pr, (v,))        # začiatok skupiny rovnakých hodnôt
            c = (abs(v - x), pr[j][1])
            if best is None or c < best:
                best = c
    return best[1]


def _mm(vals):
    """vals = [(hodnota, ts)] -> {min, avg, max} (5. a 95. percentil), pri min/max aj ts a krajné hodnoty; None pri prázdnom."""
    if not vals:
        return None
    srt = sorted(v for v, _ in vals)
    lo, hi = _pct(srt, 5), _pct(srt, 95)
    pr = sorted(vals)
    ts_lo, ts_hi = _blizky(pr, lo), _blizky(pr, hi)
    return {"min": _r1(lo), "avg": _r1(sum(srt) / len(srt)), "max": _r1(hi), "min_ts": ts_lo, "max_ts": ts_hi,
            "abs_min": _r1(srt[0]), "abs_max": _r1(srt[-1])}


def _kurenie_prazdny(d0, d1, krok):
    return {"od": d0.isoformat(), "do": d1.isoformat(), "krok": krok, "pasma": [],
            "nastavenie": {k: None for k in ("fsv2011", "fsv2012", "fsv2031", "fsv2032", "fsv4013")},
            "krivka_nastavenie": {k: None for k, _ in NAST}, "zmeny": [], "prva_vzorka": None, "minut_spolu": 0}


def kurenie(cfg, od=None, do=None, krok=1, now=None, lang="sk"):
    """Pásma vonkajšej teploty s teplotou vody do radiátorov v minútach skutočného kúrenia (SPEC_akokuri.md).
    Zdroj: tc.db (minútové vzorky + udalosti), otvára sa len na čítanie. Neplatný vstup = ValueError po slovensky."""
    try:
        krok = int(krok)
    except (TypeError, ValueError):
        raise ValueError(tr("srv.vonku.krok_125", lang))
    if krok not in (1, 2, 5):
        raise ValueError(tr("srv.vonku.krok_125", lang))
    now = int(now or time.time())
    today = _dt.date.fromtimestamp(now)
    d0 = stats._pdate(od, lang) if od else None
    d1 = stats._pdate(do, lang) if do else today
    if d0 is not None and d1 < d0:
        raise ValueError(tr("srv.vonku.koniec_pred", lang))
    if d0 is not None and (d1 - d0).days + 1 > MAX_DNI:
        raise ValueError(tr("srv.vonku.dni", lang, max=MAX_DNI))
    try:
        tc = stats._tc(cfg)
    except Exception:
        return _kurenie_prazdny(d0 or d1, d1, krok)
    try:
        try:
            first = tc.execute("SELECT MIN(ts) FROM samples").fetchone()[0]
        except Exception:
            first = None
        if first is None:
            return _kurenie_prazdny(d0 or d1, d1, krok)
        if d0 is None:
            d0 = min(_dt.date.fromtimestamp(first), d1)
            if (d1 - d0).days + 1 > MAX_DNI:
                d0 = d1 - _dt.timedelta(days=MAX_DNI - 1)
        a = stats._midnight(d0)
        b = stats._midnight(d1 + _dt.timedelta(days=1))
        return _kurenie_calc(tc, d0, d1, krok, a, b, first)
    except sqlite3.Error:
        return _kurenie_prazdny(d0 or d1, d1, krok)
    finally:
        tc.close()


def _kurenie_calc(tc, d0, d1, krok, a, b, first):
    # nastavenia krivky: posledné hodnoty pred koncom rozsahu
    nast = {}
    for k, ent in NAST:
        r = tc.execute("SELECT value FROM samples WHERE entity=? AND ts<? ORDER BY ts DESC LIMIT 1", (ent, b)).fetchone()
        nast[k] = None if r is None or r[0] is None else float(r[0])
    # minútové vzorky (s predstihom 15 min na dopĺňanie vonkajšej teploty a 10 min na rozbeh)
    base = a // 60 - 15
    n = (b + 59) // 60 - base
    ents = [E_VONKU, E_TW2, E_TW1, E_WL, E_FREQ, E_POSUN]
    arr = {e: [None] * n for e in ents}
    sledovane = {ent for k, ent in NAST if k != "posun"}      # zmeny nastavení sa hľadajú v tom istom prechode
    prev, zmeny = {}, []
    q = "SELECT ts, entity, value FROM samples WHERE ts>=? AND ts<? AND entity IN (%s) ORDER BY ts" % ",".join("?" * (len(ents) + len(sledovane)))
    for ts, ent, v in tc.execute(q, [base * 60, b] + ents + sorted(sledovane)):
        if v is None:
            continue
        if ent in sledovane:
            o = prev.get(ent)
            if o is not None and v != o and ts >= a:
                zmeny.append({"ts": ts, "nazov": ent, "old": _r1(o), "new": _r1(v)})
            prev[ent] = v
            continue
        i = ts // 60 - base
        if 0 <= i < n:
            arr[ent][i] = float(v)
    zmeny = zmeny[::-1][:MAX_ZMIEN]            # najnovšie prvé; pri rovnakom ts opačné poradie vstupu
    valve, defr = stats._tracks(tc, base * 60, b)

    bands = {}
    run = 0
    last_defr = None
    last_v = last_vi = None
    for i in range(n):
        t = (base + i) * 60
        vk = arr[E_VONKU][i]
        if vk is not None:
            last_v, last_vi = vk, i
        fr = arr[E_FREQ][i]
        run = run + 1 if (fr is not None and fr > 0) else 0
        d = defr.at(t)
        if d:
            last_defr = i
        if t < a or t >= b or last_v is None or i - last_vi > 15 or fr is None:
            continue
        vl = valve.at(t)
        if vl == "tank" or d:
            continue
        lo = int(math.floor(last_v / krok)) * krok
        bd = bands.get(lo)
        if bd is None:
            bd = bands[lo] = {"reg": 0, "beh": 0, "k": 0, "vsum": 0.0, "tw2": [], "tw1": [], "wl": [], "posun": []}
        bd["reg"] += 1
        if fr > 0:
            bd["beh"] += 1
        ps = arr[E_POSUN][i]
        if ps is not None:
            bd["posun"].append(ps)
        if not (fr > 0 and vl == "room" and run >= ROZBEH + 1 and (last_defr is None or i - last_defr >= ROZBEH)):
            continue
        bd["k"] += 1
        bd["vsum"] += last_v
        for key, e in (("tw2", E_TW2), ("tw1", E_TW1), ("wl", E_WL)):
            if arr[e][i] is not None:
                bd[key].append((arr[e][i], t))

    pasma = []
    for lo in sorted(bands):
        bd = bands[lo]
        if bd["k"] < MIN_KURENIE:
            continue
        w = lambda key: _mm(bd[key])
        tw1, wl = w("tw1"), w("wl")
        for st in (tw1, wl):
            if st:
                for k in ("min_ts", "max_ts", "abs_min", "abs_max"):
                    del st[k]
        ps = bd["posun"]
        f4013 = nast.get("fsv4013")
        pasma.append({
            "od_c": lo, "do_c": lo + krok, "minut_kurenia": bd["k"], "vonku_avg": _r1(bd["vsum"] / bd["k"]),
            "tw2": w("tw2"), "tw1": tw1, "wl": wl,
            "posun": {"min": _r1(min(ps)), "max": _r1(max(ps))} if ps else None,
            "beh_pct": int(round(bd["beh"] * 100.0 / bd["reg"])) if bd["reg"] else None,
            "krivka": _krivka(lo + krok / 2.0, nast),
            "nad_vypnutim": bool(f4013 is not None and lo >= f4013)})
    return {"od": d0.isoformat(), "do": d1.isoformat(), "krok": krok, "pasma": pasma,
            "nastavenie": {k: nast[k] for k in ("fsv2011", "fsv2012", "fsv2031", "fsv2032", "fsv4013")},
            "krivka_nastavenie": nast, "zmeny": zmeny, "prva_vzorka": stats._dstr(first),
            "minut_spolu": sum(p["minut_kurenia"] for p in pasma)}

