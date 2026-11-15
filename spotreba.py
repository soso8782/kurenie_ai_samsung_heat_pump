#!/usr/bin/env python3
"""Denná spotreba elektriny domu a odhad ceny (SPEC_spotreba.md): záložka Spotreba, GET /api/spotreba.

Zdroje: stats.db (tabuľka hourly: kwh_dom z elektromera, čerpadlo = COALESCE(kwh_cal, kwh), q_kwh, vonku) a história z portálu
distribúcie (ims_hourly.json cez ha_cost.load_ims). Kde má stats.db hodnotu kwh_dom, má prednosť; inak sa použije ims.
Cena: ha_cost.tariff_at (vážená cena s DPH + fixná platba na hodinu). Deň sa počíta cez lokálnu polnoc (23–25 hodín).
stats.db sa otvára len na čítanie. Iba štandardná knižnica."""
import datetime as _dt, os, time

import ha_cost
import stats

MAX_DNI = 400
MIN_HODIN_CERPADLO = 20      # ostatné = dom − čerpadlo len ak má čerpadlo aspoň toľko hodín za deň
MIN_KWH_COP = 0.5            # COP sa počíta od tejto spotreby čerpadla za deň
DNI_TYZDNA = ("po", "ut", "st", "št", "pi", "so", "ne")

_ims_cache = {}              # {cesta: (mtime, veľkosť, slovník)}


def _ims(path):
    """História domu z ims_hourly.json; načíta sa raz a znova až pri zmene súboru (mtime/veľkosť)."""
    try:
        stt = os.stat(path)
        key = (stt.st_mtime_ns, stt.st_size)
    except OSError:
        return {}
    c = _ims_cache.get(path)
    if c and c[0] == key:
        return c[1]
    d = ha_cost.load_ims(path)
    _ims_cache[path] = (key, d)
    return d


def _r(v, n=2):
    return None if v is None else round(v, n)


def _hourly(cfg, a, b):
    """{ts: (kwh_dom, čerpadlo, q_kwh, vonku)} zo stats.db pre [a, b) jedným SELECTom; pri chýbajúcej DB/stĺpcoch prázdne."""
    try:
        con = stats._ro(stats._db_path(cfg), stats._timeout(cfg))
    except Exception:
        return {}
    try:
        have = {r[1] for r in con.execute("PRAGMA table_info(hourly)")}
        if not have:
            return {}
        sel = lambda c: c if c in have else "NULL"
        if "kwh_cal" in have or "kwh" in have:
            pump = "COALESCE(%s, %s)" % (sel("kwh_cal"), sel("kwh"))
        else:
            pump = "NULL"
        rows = con.execute("SELECT ts, %s, %s, %s, %s FROM hourly WHERE ts>=? AND ts<? ORDER BY ts"
                           % (sel("kwh_dom"), pump, sel("q_kwh"), sel("vonku")), (a, b)).fetchall()
    except Exception:
        return {}
    finally:
        con.close()
    return {r[0]: r[1:] for r in rows}


def _month_key(d):
    return "%04d-%02d" % (d.year, d.month)


def report(cfg, od, do, now=None):
    """Spotreba po dňoch za obdobie od–do (YYYY-MM-DD vrátane, max MAX_DNI dní; None = predvolené: od začiatku fakturačného
    obdobia po dnes). Neplatný vstup vyhodí ValueError so slovenským textom."""
    import ha_pub
    now = int(now or time.time())
    bs = _dt.date.fromtimestamp(ha_pub.billing_start(cfg, now))
    d0 = stats._pdate(od) if od else bs
    d1 = stats._pdate(do) if do else _dt.date.fromtimestamp(now)
    if d1 < d0:
        raise ValueError("Koniec obdobia je pred začiatkom.")
    n = (d1 - d0).days + 1
    if n > MAX_DNI:
        raise ValueError("Obdobie je príliš dlhé (%d dní, najviac %d)." % (n, MAX_DNI))
    a = stats._midnight(d0)
    b = stats._midnight(d1 + _dt.timedelta(days=1))
    st = _hourly(cfg, a, b)
    ims = _ims(ha_cost.IMS_FILE)

    dni = []
    for i in range(n):
        d = d0 + _dt.timedelta(days=i)
        da, db = stats._bounds(d)
        hodin = int(round((db - da) / 3600.0))
        price, fee = ha_cost.tariff_at(cfg, da)       # cena aj poplatok sa v rámci lokálneho dňa nemenia
        dom = pump = teplo = None
        n_dom = n_pump = 0
        vonku_s, vonku_n = 0.0, 0
        cena_dom = cena_pump = 0.0
        for h in range(da, db, 3600):
            r = st.get(h)
            kd = r[0] if r and r[0] is not None else ims.get(h)
            if kd is not None:
                kd = max(0.0, kd)
                dom = (dom or 0.0) + kd
                n_dom += 1
                cena_dom += kd * price
            if r:
                if r[1] is not None:
                    kp = max(0.0, r[1])
                    pump = (pump or 0.0) + kp
                    n_pump += 1
                    cena_pump += kp * price
                if r[2] is not None:
                    teplo = (teplo or 0.0) + r[2]
                if r[3] is not None:
                    vonku_s += r[3]
                    vonku_n += 1
        ostatne = (dom - pump) if dom is not None and pump is not None and n_pump >= MIN_HODIN_CERPADLO else None
        cop = (teplo / pump) if teplo is not None and pump is not None and pump >= MIN_KWH_COP else None
        cena = (cena_dom + fee * hodin) if n_dom >= 1 else None
        dni.append({"datum": d.isoformat(), "den_tyzdna": DNI_TYZDNA[d.weekday()], "hodin": hodin,
                    "dom_kwh": _r(dom), "dom_hodin": n_dom, "kompletny": n_dom == hodin,
                    "cerpadlo_kwh": _r(pump), "cerpadlo_hodin": n_pump, "ostatne_kwh": _r(ostatne),
                    "teplo_kwh": _r(teplo), "cop": _r(cop),
                    "vonku": _r(vonku_s / vonku_n, 1) if vonku_n else None,
                    "cena_eur": _r(cena), "cena_cerpadlo_eur": _r(cena_pump if pump is not None else None),
                    # nezaokrúhlené hodnoty pre súčty
                    "_dom": dom, "_pump": pump, "_ost": ostatne, "_cena": cena, "_cp": cena_pump if pump is not None else None})

    def ssum(rows, key):
        v = [x[key] for x in rows if x[key] is not None]
        return sum(v) if v else None

    s_dom, s_pump = ssum(dni, "_dom"), ssum(dni, "_pump")
    s_cena, s_cp = ssum(dni, "_cena"), ssum(dni, "_cp")
    s_ost = ssum(dni, "_ost")
    n_dni = sum(1 for x in dni if x["_dom"] is not None)
    n_cena = sum(1 for x in dni if x["_cena"] is not None)
    spolu = {"dom_kwh": _r(s_dom), "cerpadlo_kwh": _r(s_pump), "ostatne_kwh": _r(s_ost), "cena_eur": _r(s_cena),
             "cena_cerpadlo_eur": _r(s_cp), "dni_s_udajmi": n_dni,
             "priemer_den_kwh": _r(s_dom / n_dni) if n_dni else None,
             "priemer_den_eur": _r(s_cena / n_cena) if n_cena else None}

    mes = {}
    for x in dni:
        mes.setdefault(x["datum"][:7], []).append(x)
    mesiace = [{"mesiac": k, "dom_kwh": _r(ssum(v, "_dom")), "cerpadlo_kwh": _r(ssum(v, "_pump")),
                "cena_eur": _r(ssum(v, "_cena")), "dni_s_udajmi": sum(1 for x in v if x["_dom"] is not None)}
               for k, v in sorted(mes.items())]

    # cenník platný v posledný deň rozsahu
    la = stats._midnight(d1)
    price, fee = ha_cost.tariff_at(cfg, la)
    ep = str(cfg.get("ENERGY_ENERGOPOMOC", "0")).strip() == "1"
    row = ha_cost.TARIFF["s" if ep else "bez"][0]
    for r in ha_cost.TARIFF["s" if ep else "bez"]:
        if r[0] <= d1.isoformat():
            row = r
    cennik = {"vazena_cena": _r(price, 4), "poplatok_den": _r(fee * 24, 4), "vt": row[1], "nt": row[2],
              "mesacna_platba": _r(ha_cost._f(cfg, "ENERGY_FEE_MONTH", row[3], 0.0, 200.0), 4), "energopomoc": ep,
              "vt_podiel": ha_cost._f(cfg, "ENERGY_VT_SHARE", 0.09, 0.0, 1.0), "platny_od": row[0]}

    posledny = None
    for x in dni:
        if x["_dom"] is not None:
            posledny = x["datum"]
    for x in dni:
        for k in [k for k in x if k.startswith("_")]:
            del x[k]
    return {"od": d0.isoformat(), "do": d1.isoformat(), "dni": dni, "spolu": spolu, "mesiace": mesiace, "cennik": cennik,
            "posledny_den_s_udajmi": posledny, "obdobie_zaciatok": bs.isoformat()}
