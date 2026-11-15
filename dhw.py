#!/usr/bin/env python3
"""Plánovač ohrevu vody (TÚV) - SPEC bod 6. Predvolene VYPNUTÝ (state.dhw_plan = "0"). Deterministický, bez modelu.

V najteplejšom 2-hodinovom okne dňa (10:00-16:00 podľa predpovede) zvýši "Cieľovú teplotu TÚV" o 2 °C (najviac na 52 °C),
na konci okna vráti pôvodnú hodnotu. Jediná entita, ktorú tento modul zapisuje, je "Cieľová teplota TÚV" v rozsahu 40-52 °C.
Zápis ide VÝLUČNE cez agent.esp_set_number() (ten rešpektuje DRY_RUN). Volá sa z watchdogu každú minútu: tick().
"""
import json, time, urllib.request
import abtest, notify
from common import S, get_state, is_dry, set_state, tcq

ENT_TARGET = "Cieľová teplota TÚV"
ENT_BOOSTER = "Elektrický dohrev TÚV (Booster heater)"
ENT_ERROR = "Chybový kód TČ"
T_MIN, T_MAX, T_STEP = 40.0, 52.0, 2.0
DEFAULT_WINDOW = ("12:00", "14:00")
STALE_S = 600
REASON_UP = "plánovač TÚV: prihriatie v okne"
REASON_DOWN = "plánovač TÚV: návrat na pôvodnú hodnotu"
MANUAL_MAX_S = 3 * 3600      # manuálny ohrev z webu: najdlhšie 3 hodiny
MANUAL_GAP_S = 30 * 60       # odstup medzi dvoma štartmi
MANUAL_MIN, MANUAL_MAX_T = 45.0, 52.0


def _num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _agent():
    import agent   # oneskorený import (agent importuje tento modul)
    return agent


def enabled(c):
    return get_state(c, "dhw_plan", "0") == "1"


def load_window(c):
    try:
        w = json.loads(get_state(c, "dhw_window", "") or "null")
        if isinstance(w, dict) and w.get("date") and w.get("start") and w.get("end"):
            return w
    except ValueError:
        pass
    return None


def today_window(c, now=None):
    """Dnešné okno alebo None (pre web)."""
    w = load_window(c)
    return w if w and w["date"] == time.strftime("%Y-%m-%d", time.localtime(now or time.time())) else None


def in_window(w, now):
    if not w or w.get("date") != time.strftime("%Y-%m-%d", time.localtime(now)):
        return False
    hm = time.strftime("%H:%M", time.localtime(now))
    return w["start"] <= hm < w["end"]


def pick_window(temps):
    """temps: {hodina(int): teplota}. 2-hodinové okno s najvyšším priemerom medzi 10:00-16:00 (začiatok 10..14); remíza = skôr."""
    best = None
    for s in range(10, 15):
        if s in temps and s + 1 in temps and temps[s] is not None and temps[s + 1] is not None:
            avg = (temps[s] + temps[s + 1]) / 2.0
            if best is None or avg > best[0] + 1e-9:
                best = (avg, s)
    if not best:
        return None
    return "%02d:00" % best[1], "%02d:00" % (best[1] + 2)


def fetch_temps(cfg, date):
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s&hourly=temperature_2m&forecast_days=2&timezone=auto"
           % (cfg["LAT"], cfg["LON"]))
    with urllib.request.urlopen(url, timeout=15) as r:
        h = json.loads(r.read().decode("utf-8"))["hourly"]
    out = {}
    for t, v in zip(h["time"], h["temperature_2m"]):
        if t.startswith(date):
            out[int(t[11:13])] = v
    return out


def ensure_window(cfg, c, now=None):
    """Raz denne po 06:00 vyberie dnešné okno a uloží state.dhw_window. Vracia okno alebo None."""
    now = now or time.time()
    lt = time.localtime(now)
    today = time.strftime("%Y-%m-%d", lt)
    w = load_window(c)
    if w and w["date"] == today:
        return w
    if lt.tm_hour < 6:
        return None
    start = end = None
    if cfg.get("LAT") and cfg.get("LON"):
        last_try = float(get_state(c, "dhw_fetch_try", "0") or 0)
        if now - last_try < 300:
            return None
        try:
            set_state(c, "dhw_fetch_try", now)
            win = pick_window(fetch_temps(cfg, today))
            if win:
                start, end = win
        except Exception as e:
            print("plánovač TÚV: predpoveď nedostupná (%s)" % type(e).__name__, flush=True)
            if lt.tm_hour < 9:
                return None   # ešte skúsime znova; po 09:00 použijeme predvolené okno
    if not start:
        start, end = DEFAULT_WINDOW
    w = {"date": today, "start": start, "end": end}
    set_state(c, "dhw_window", json.dumps(w))
    return w


def _log_action(c, old, new, reason, applied, result):
    c.execute("INSERT INTO actions(ts, entity, old, new, reason, applied, result) VALUES(?,?,?,?,?,?,?)",
              (int(time.time()), ENT_TARGET, old, new, reason, applied, result))
    c.commit()


def _write_target(cfg, value):
    """Jediný zápis tohto modulu: cieľ TÚV v rozsahu 40-52 °C cez agent.esp_set_number (DRY_RUN sa rešpektuje tam)."""
    if not (T_MIN <= value <= T_MAX):
        raise ValueError("hodnota TÚV %s mimo povoleného rozsahu %s-%s" % (value, T_MIN, T_MAX))
    ag = _agent()
    ag.esp_set_number(cfg, ENT_TARGET, "%g" % value)
    if is_dry(cfg):
        return True
    time.sleep(3)
    back = _num(ag.esp_get(cfg, "number", ENT_TARGET).get("value"))
    return back is not None and abs(back - value) < 1e-6


def can_start(cfg, c):
    """(ok, dôvod): agent nie je off, chybový kód 0, dáta čerstvé, booster vypnutý."""
    ag = _agent()
    if abtest.active(c):
        return False, "prebieha porovnávací test"
    if get_state(c, "mode", "advise") == "off":
        return False, "agent je vypnutý"
    r = tcq(cfg, "SELECT v FROM meta WHERE k='last_sample'")
    age = int(time.time()) - int((r[0][0] if r else 0) or 0)
    if age > STALE_S:
        return False, "dáta zberača sú staré (%d s)" % age
    code = _num(ag.esp_get(cfg, "sensor", ENT_ERROR).get("value"))
    if code is None or code != 0:
        return False, "chybový kód TČ = %s" % code
    if ag.esp_get(cfg, "binary_sensor", ENT_BOOSTER).get("value") is not False:
        return False, "elektrický dohrev TÚV je zapnutý"
    return True, ""


def start_boost(cfg, c, w):
    """Prihriatie na začiatku okna. Vracia text výsledku."""
    ok, why = can_start(cfg, c)
    if not ok:
        return "čaká: " + why
    base = _num(_agent().esp_get(cfg, "number", ENT_TARGET).get("value"))
    marker = "%s %s" % (w["date"], w["start"])
    if base is None:
        return "čaká: cieľ TÚV sa nepodarilo prečítať"
    if base >= T_MAX or base < T_MIN:
        set_state(c, "dhw_done", marker)
        return "nič: pôvodný cieľ TÚV %s mimo 40-52 (alebo už 52)" % base
    new = min(base + T_STEP, T_MAX)
    set_state(c, "dhw_base", base)
    set_state(c, "dhw_boost_value", new)
    set_state(c, "dhw_done", marker)
    try:
        good = _write_target(cfg, new)
    except Exception as e:
        _log_action(c, base, new, REASON_UP, 0, "chyba zápisu: %s" % e)
        set_state(c, "dhw_boosted", "0")
        return "chyba zápisu: %s" % e
    set_state(c, "dhw_boosted", "1")    # aj pri nepotvrdenom zápise: na konci okna sa hodnota vráti
    _log_action(c, base, new, "%s %s-%s" % (REASON_UP, w["start"], w["end"]), 1 if good else 0,
                ("DRY_RUN (neodoslané)" if is_dry(cfg) else "zapísané a overené") if good else "zápis NEPOTVRDENÝ")
    return "prihriate %s -> %s" % (base, new)


def restore(cfg, c, why):
    """Vráti pôvodný cieľ TÚV. Ak ho medzitým niekto zmenil ručne (nie je to naša hodnota), nechá ho tak. Vracia text."""
    base = _num(get_state(c, "dhw_base"))
    bv = _num(get_state(c, "dhw_boost_value"))
    if base is None or not (T_MIN <= base <= T_MAX):
        set_state(c, "dhw_boosted", "0")
        return "nie je čo vracať"
    ag = _agent()
    cur = None if is_dry(cfg) else _num(ag.esp_get(cfg, "number", ENT_TARGET).get("value"))
    if not is_dry(cfg) and cur is None:
        raise RuntimeError("cieľ TÚV sa nepodarilo prečítať, skúsim znova")
    if is_dry(cfg) or bv is None or abs(cur - bv) < 1e-6:
        good = _write_target(cfg, base)
        _log_action(c, bv, base, "%s (%s)" % (REASON_DOWN, why), 1 if good else 0,
                    ("DRY_RUN (neodoslané)" if is_dry(cfg) else "zapísané a overené") if good else "zápis NEPOTVRDENÝ")
        res = "vrátené na %s (%s)" % (base, why)
    else:
        _log_action(c, cur, cur, "%s (%s)" % (REASON_DOWN, why), 0, "hodnotu medzitým zmenil používateľ (%s) - ponechané" % cur)
        res = "ponechané: používateľ zmenil cieľ na %s" % cur
    set_state(c, "dhw_boosted", "0")
    return res


def _fmt(v):
    return ("%g" % v).replace(".", ",")


def manual_status(c, now=None):
    """Stav manuálneho ohrevu z webu (pre /api/status): active, until, target, base, last."""
    now = now or time.time()
    boosted = get_state(c, "dhw_boosted", "0") == "1"
    until = _num(get_state(c, "dhw_manual_until", ""))
    active = bool(boosted and until is not None and until > now)
    return {"active": active, "until": int(until) if active else None,
            "target": _num(get_state(c, "dhw_boost_value")) if active else None,
            "base": _num(get_state(c, "dhw_base")) if boosted else None,
            "last": (int(_num(get_state(c, "dhw_manual_last", ""))) if _num(get_state(c, "dhw_manual_last", "")) is not None else None)}


def manual_start(cfg, c, target, who, now=None):
    """Manuálny ohrev z webu: dočasne zvýši cieľ TÚV (45-52, krok 1). Vracia (ok, text)."""
    now = now or time.time()
    t = _num(target)
    if isinstance(target, bool) or t is None or not (MANUAL_MIN <= t <= MANUAL_MAX_T) or abs(t - round(t)) > 1e-9:
        return False, "Teplota musí byť 45 až 52 °C po 1 °C."
    t = float(round(t))
    ok, why = can_start(cfg, c)
    if not ok:
        return False, "Ohrev teraz nejde: %s." % why
    last = _num(get_state(c, "dhw_manual_last", ""))
    if last is not None and now - last < MANUAL_GAP_S:
        return False, "Ďalší ohrev môžeš spustiť o %d min." % max(1, int((MANUAL_GAP_S - (now - last) + 59) // 60))
    was_boosted = get_state(c, "dhw_boosted", "0") == "1"
    old_until = get_state(c, "dhw_manual_until", "")
    if was_boosted and _num(get_state(c, "dhw_base")) is not None:
        base = _num(get_state(c, "dhw_base"))
    else:
        try:
            base = _num(_agent().esp_get(cfg, "number", ENT_TARGET).get("value"))
        except Exception:
            base = None
        if base is None:
            return False, "Cieľ TÚV sa nepodarilo prečítať."
    if t <= base:
        return False, "Cieľ TÚV je už %s °C, zvoľ vyššiu teplotu." % _fmt(base)
    old_bv = get_state(c, "dhw_boost_value", "")
    set_state(c, "dhw_base", base)
    set_state(c, "dhw_boost_value", t)
    set_state(c, "dhw_manual_until", int(now + MANUAL_MAX_S))
    set_state(c, "dhw_manual_last", int(now))
    reason = "manuálny ohrev TÚV z webu (%s)" % who
    try:
        good = _write_target(cfg, t)
    except Exception as e:
        set_state(c, "dhw_boost_value", old_bv)
        set_state(c, "dhw_manual_until", old_until)
        set_state(c, "dhw_boosted", "1" if was_boosted else "0")
        _log_action(c, base, t, reason, 0, "chyba zápisu: %s" % e)
        return False, "Zápis zlyhal: %s" % e
    set_state(c, "dhw_boosted", "1")    # aj pri nepotvrdenom zápise: na konci sa hodnota vráti
    _log_action(c, base, t, reason, 1 if good else 0,
                ("DRY_RUN (neodoslané)" if is_dry(cfg) else "zapísané a overené") if good else "zápis NEPOTVRDENÝ")
    text = "Ohrev spustený: cieľ TÚV %s °C, najdlhšie do %s." % (_fmt(t), time.strftime("%H:%M", time.localtime(now + MANUAL_MAX_S)))
    if is_dry(cfg):
        text += " (testovacie prostredie, nezapísané)"
    return True, text


def manual_stop(cfg, c, who):
    """Zastaví manuálny ohrev a vráti pôvodný cieľ. Vracia (ok, text)."""
    if _num(get_state(c, "dhw_manual_until", "")) is None:
        return False, "Manuálny ohrev nebeží."
    base = _num(get_state(c, "dhw_base"))
    try:
        restore(cfg, c, "zastavené z webu (%s)" % who)
    except RuntimeError as e:
        return False, "Ohrev sa nepodarilo zastaviť: %s." % e
    set_state(c, "dhw_manual_until", "")
    return True, "Ohrev zastavený, cieľ TÚV vrátený na %s °C." % (_fmt(base) if base is not None else "?")


def set_target_manual(cfg, c, value, who):
    """Trvalá zmena cieľa TÚV z webu (správca; 40-52, celé číslo, nie počas ohrevu). Vracia (ok, text)."""
    if get_state(c, "dhw_boosted", "0") == "1":
        return False, "Počas ohrevu cieľ nemeň, najprv ho zastav."
    v = _num(value)
    if isinstance(value, bool) or v is None or not (T_MIN <= v <= T_MAX) or abs(v - round(v)) > 1e-9:
        return False, "Cieľ TÚV musí byť celé číslo od 40 do 52 °C."
    v = float(round(v))
    old = None
    if not is_dry(cfg):
        try:
            old = _num(_agent().esp_get(cfg, "number", ENT_TARGET).get("value"))
        except Exception:
            old = None
    reason = "ručná zmena cieľa TÚV z webu (%s)" % who
    try:
        good = _write_target(cfg, v)
    except Exception as e:
        _log_action(c, old, v, reason, 0, "chyba zápisu: %s" % e)
        return False, "Zápis zlyhal: %s" % e
    _log_action(c, old, v, reason, 1 if good else 0,
                ("DRY_RUN (neodoslané)" if is_dry(cfg) else "zapísané a overené") if good else "zápis NEPOTVRDENÝ")
    if not good:
        return False, "Zápis cieľa TÚV sa nepodarilo potvrdiť."
    return True, "Cieľ TÚV je %s °C." % _fmt(v)


def _bojler_now(cfg, now):
    """Posledná teplota bojlera zo samples (vek <= STALE_S) alebo None."""
    try:
        r = tcq(cfg, "SELECT ts, value FROM samples WHERE entity=? AND value IS NOT NULL ORDER BY ts DESC LIMIT 1", (S["bojler"],))
    except Exception:
        return None
    if not r or now - int(r[0][0]) > STALE_S or abs(r[0][1]) > 6000:
        return None
    return float(r[0][1])


def _tick_manual(cfg, c, now):
    """Vetva ticku pre manuálny ohrev z webu (dhw_manual_until je číslo)."""
    until = _num(get_state(c, "dhw_manual_until", ""))
    try:
        booster_on = _agent().esp_get(cfg, "binary_sensor", ENT_BOOSTER).get("value") is True
    except Exception:
        booster_on = False
    if booster_on:
        res = restore(cfg, c, "zapol sa elektrický dohrev TÚV")
        set_state(c, "dhw_manual_until", "")
        notify.event(cfg, "tuv", "Kúrenie: manuálny ohrev TÚV zrušený",
                     "Manuálny ohrev vody bol zrušený, pretože sa zapol elektrický dohrev TÚV (booster). Cieľová teplota TÚV: %s." % res)
        return "manuálny ohrev: booster: " + res
    if now >= until:
        res = restore(cfg, c, "koniec času ohrevu")
        set_state(c, "dhw_manual_until", "")
        return "manuálny ohrev: koniec času: " + res
    bv = _num(get_state(c, "dhw_boost_value"))
    b = _bojler_now(cfg, now)
    if b is not None and bv is not None and b >= bv - 0.5:
        res = restore(cfg, c, "bojler dosiahol cieľ")
        set_state(c, "dhw_manual_until", "")
        return "manuálny ohrev: bojler dosiahol cieľ: " + res
    return "manuálny ohrev beží"


def tick(cfg, c, now=None):
    """Jeden krok plánovača (z watchdogu každú minútu). Vracia krátky popis, čo spravil (pre log/testy)."""
    now = now or time.time()
    w = ensure_window(cfg, c, now)
    boosted = get_state(c, "dhw_boosted", "0") == "1"
    on = enabled(c)
    inw = in_window(w, now)
    if boosted:
        if _num(get_state(c, "dhw_manual_until", "")) is not None:
            return _tick_manual(cfg, c, now)
        # poistka: zapnutý booster počas prihriatia
        try:
            booster_on = _agent().esp_get(cfg, "binary_sensor", ENT_BOOSTER).get("value") is True
        except Exception:
            booster_on = False
        if booster_on:
            res = restore(cfg, c, "zapol sa elektrický dohrev TÚV")
            set_state(c, "dhw_plan", "0")
            base = _num(get_state(c, "dhw_base"))
            notify.event(cfg, "tuv", "Kúrenie: plánovač TÚV zrušený",
                         "Plánovač ohrevu vody bol vypnutý, pretože sa zapol elektrický dohrev TÚV (booster). Cieľová teplota TÚV: %s." % res)
            return "booster: " + res
        if not on:
            return "vypnutý z webu: " + restore(cfg, c, "plánovač vypnutý")
        if not inw:
            return "koniec okna: " + restore(cfg, c, "koniec okna")
        return "prihriate, čaká na koniec okna"
    if on and inw:
        marker = "%s %s" % (w["date"], w["start"])
        if get_state(c, "dhw_done", "") == marker:
            return "okno už spracované"
        return start_boost(cfg, c, w)
    return "nečinný"
