#!/usr/bin/env python3
"""profil.py – profil inštalácie (verzia 2): všetko, čo je špecifické pre konkrétny dom a tepelné čerpadlo.

Súbor `profil.json` v ROOT (nie je v gite). Ak chýba, platí `default()` = pôvodná inštalácia (Samsung AE120JXEDGH Split 12 kW,
radiátory, 7 izieb) – správanie je potom totožné s verziou 1.x. Zmena profilu sa prejaví po reštarte služieb
(moduly si mapy načítajú pri importe).

Verejné API (ostatné moduly používajú LEN toto):
    load(path=None) -> dict            platný, normalizovaný profil (cache podľa mtime)
    validate(p) -> (dict, [chyby])     normalizácia + zoznam chýb po slovensky (prázdny = OK)
    save(p, path=None) -> [chyby]      overí a atomicky zapíše (chmod 600); pri chybách nič nezapíše
    default() -> dict                  predvolený profil
    esp_s(p) / esp_tc(p)               mapy kľúč -> názov entity v ESP, kompatibilné s common.S / stats.TC
    settings_map(p)                    kompatibilné s common.SETTINGS
    room_keys(p), room_names(p), reference_room(p), target_rooms(p), target_defaults(p)
    limits(p) -> {názov entity ESP: (min, max, max_krok, hodín, zaokrúhlenie)}   kompatibilné s agent.ALLOWED
    room_target_range(p) -> (min, max)
    model(p) -> dict | None            tabuľka výrobcu z modely/<id>.json
    ha_columns(p) -> [(stĺpec, [entity_id…])]   zdroje histórie z HA (kompatibilné so stats_backfill.COLUMN_IDS)
Iba štandardná knižnica.
"""
import copy, json, os, re

ROOT = os.path.dirname(os.path.abspath(__file__))
PROFILE_FILE = os.path.join(ROOT, "profil.json")
MODELS_DIR = os.path.join(ROOT, "modely")
VERSION = 1

KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,20}$")
# kľúče izieb nesmú kolidovať so stĺpcami tabuľky hourly v stats.db
RESERVED = {"ts", "n", "src", "vonku", "vonku_min", "vonku_max", "ciel", "tw2", "tw1", "wl", "dt_beh", "tuv", "tuv_ciel", "tuv_min",
            "beh_min", "kurenie_min", "odmraz_min", "starty", "hz_avg", "hz_max", "kwh", "prikon", "updated", "kwh_dom", "kwh_cal",
            "q_kwh", "cop", "posun", "bojler", "dt", "freq", "pwm", "chyba", "spotreba", "flow", "beh"}

# Absolútne hranice – profil ich môže len zúžiť, nikdy rozšíriť (bezpečnosť zariadenia a domu).
ABS_LIMITS = {
    "fsv2031": (25.0, 55.0),    # voda pri najchladnejšom
    "fsv2032": (20.0, 45.0),    # voda pri najteplejšom
    "fsv4013": (10.0, 35.0),    # vypnutie kúrenia nad vonk. teplotou
    "max_posun": (0.0, 5.0),    # maximálny kladný posun krivky
}
ABS_ROOM_TARGET = (16.0, 26.0)

# Predvolené mantinely agenta podľa typu vykurovania: (min, max, max_krok, hodín medzi zmenami, zaokrúhlenie)
PRESETS = {
    "radiatory": {"fsv2031": (42.0, 50.0, 1.0, 24, 1.0), "fsv2032": (32.0, 38.0, 1.0, 24, 1.0),
                  "fsv4013": (17.0, 24.0, 2.0, 24, 1.0), "max_posun": (1.0, 4.0, 0.5, 12, 0.5)},
    "podlahove": {"fsv2031": (30.0, 40.0, 1.0, 24, 1.0), "fsv2032": (24.0, 32.0, 1.0, 24, 1.0),
                  "fsv4013": (15.0, 22.0, 2.0, 24, 1.0), "max_posun": (0.5, 2.0, 0.5, 12, 0.5)},
    "kombinovane": {"fsv2031": (35.0, 45.0, 1.0, 24, 1.0), "fsv2032": (28.0, 35.0, 1.0, 24, 1.0),
                    "fsv4013": (16.0, 23.0, 2.0, 24, 1.0), "max_posun": (0.5, 3.0, 0.5, 12, 0.5)},
}
TYPY_KURENIA = tuple(PRESETS)
TYPY_JEDNOTKY = ("split", "mono", "hydrosplit")

# kanonické kľúče entít zariadenia v ESP (hodnoty = názvy entít v ESPHome)
DEVICE_KEYS = ("vonku", "sledovana", "ciel_akt", "posun", "wl", "tw2", "tw1", "dt", "freq", "prikon", "tuv", "tuv_ciel", "pwm",
               "chyba", "kwh_den", "flow", "ventil", "odmrazovanie", "termostat", "ciel_izby")
SETTING_KEYS = ("fsv2011", "fsv2012", "fsv2021", "fsv2022", "fsv2031", "fsv2032", "fsv4013", "max_posun", "max_posun_zaporny",
                "zosilnenie")


def default():
    """Pôvodná inštalácia (verzia 1.x)."""
    tcp = "sensor.technicka_miestnost_m5stack_tc_"
    return {
        "verzia": VERSION,
        "zariadenie": {"model": "AE120JXEDGH", "nazov": "Samsung EHS Split 12 kW (AE120JXEDGH + AE160JNYDGH)", "typ": "split",
                       "vykon_kw": 12.0, "fazy": 3, "chladivo": "R410A", "kurenie": "radiatory", "tuv": True},
        "esp": {
            "entity": {
                "vonku": "Vonkajšia teplota", "sledovana": "Sledovaná teplota (Detská izba)",
                "ciel_akt": "Aktuálna cieľová teplota izby", "posun": "Posun ekvitermickej krivky",
                "wl": "Cieľová ekvitermická voda (WL)", "tw2": "Výstupná teplota vody (Tw2)", "tw1": "Spiatočka (Tw1)",
                "dt": "Delta T vykurovacej vody", "freq": "Frekvencia kompresora", "prikon": "Odhadovaný príkon TČ",
                "tuv": "Teplota TÚV (Bojler)", "tuv_ciel": "Cieľová teplota TÚV", "pwm": "Otáčky obehového čerpadla (PWM)",
                "chyba": "Chybový kód TČ", "kwh_den": "Denná spotreba TČ", "flow": "Prietok vody",
                "ventil": "3-cestný ventil (Smer ohrevu)", "odmrazovanie": "Odmrazovanie",
                "termostat": "Termostat (požiadavka na teplo)", "ciel_izby": "Cieľová izbová teplota",
            },
            "nastavenia": {
                "fsv2011": "FSV 2011 Vonku najchladnejšie", "fsv2012": "FSV 2012 Vonku najteplejšie",
                "fsv2021": "FSV 2021 WL1 voda pri najchladnejšom", "fsv2022": "FSV 2022 WL1 voda pri najteplejšom",
                "fsv2031": "FSV 2031 WL2 voda pri najchladnejšom", "fsv2032": "FSV 2032 WL2 voda pri najteplejšom",
                "fsv4013": "FSV 4013 Vypnutie kúrenia nad vonk. teplotou", "max_posun": "Maximálny kladný posun krivky",
                "max_posun_zaporny": "Maximálny posun krivky", "zosilnenie": "Zosilnenie posunu krivky",
            },
            "rest_extra": ["Radiátor Jedáleň (hlavica)"],   # ďalšie entity, ktoré zberač číta cez REST
        },
        "izby": [
            {"kluc": "detska", "nazov": "Detská", "esp": "Izba Detská (Zigbee)", "referencna": True, "min": None,
             "ha": ["sensor.detska_izba_espresense_detska_izba_detska_izba_teplota", "sensor.detska_t_h_temperature"]},
            {"kluc": "jedalen", "nazov": "Jedáleň", "esp": "Izba Jedáleň", "min": 22.5, "ha": ["sensor.0x00124b0029114db8_temperature"]},
            {"kluc": "kuchyna", "nazov": "Kuchyňa", "esp": "Izba Kuchyňa", "min": 22.5, "ha": ["sensor.t_h_kuchyna_temperature"]},
            {"kluc": "obyvacka", "nazov": "Obývačka", "esp": "Izba Obývačka", "min": 22.0, "ha": ["sensor.obyvacka_t_h_cidlo_temperature"]},
            {"kluc": "spalna", "nazov": "Spálňa", "esp": "Izba Spálňa", "min": 21.0, "ha": ["sensor.spalna_t_h_cidlo_2_kruhovy_temperature"]},
            {"kluc": "pracovna", "nazov": "Pracovňa", "esp": "Izba Pracovňa", "min": 21.0, "ha": ["sensor.pracovna_t_h_cidlo_temperature"]},
            {"kluc": "kupelna", "nazov": "Kúpeľňa", "esp": "Izba Kúpeľňa", "min": 22.0, "ha": ["sensor.kupelna_t_h_temperature"]},
        ],
        # ďalšie teploty len na sledovanie (nie izby pre reguláciu): stĺpec v stats.db -> HA entity
        "ine_teploty": {"chodba": ["sensor.t_h_sensor_chodba_teplota"], "kotolna": ["sensor.t_h_kotolna_teplota"]},
        "ha": {
            "vonku": ["sensor.irimav20_temperature", "sensor.m5stack_tc_vonkajsia_teplota_2"],
            "tw2": [tcp + "vystupna_teplota_vody_tw2"], "tw1": [tcp + "spiatocka_tw1"],
            "wl": [tcp + "cielova_ekvitermicka_voda_wl"], "tuv": [tcp + "teplota_tuv_bojler"],
        },
        "mantinely": {"ciel_izby": [20.0, 24.5]},   # fsv*/max_posun chýbajú = predvolené podľa typu kúrenia
        "elektrina": {"ims_stat": "", "meter_ids": []},   # prázdne = premenné prostredia / vypnuté
    }


# ---------------------------------------------------------------- validácia
def _num(v, lo=None, hi=None):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x or (lo is not None and x < lo) or (hi is not None and x > hi):
        return None
    return x


def _strlist(v):
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    return [s.strip() for s in v if isinstance(s, str) and s.strip()]


def validate(p):
    """Doplní chýbajúce časti z default(), normalizuje typy a vráti (profil, chyby). Pri chybách je vrátený profil stále
    použiteľný (neplatné položky sú nahradené predvolenými), ale save() ho nezapíše."""
    d = default()
    err = []
    if not isinstance(p, dict):
        return d, ["Profil musí byť JSON objekt."]
    out = copy.deepcopy(d)
    out["verzia"] = VERSION

    z = p.get("zariadenie") or {}
    if not isinstance(z, dict):
        err.append("zariadenie: musí byť objekt.")
        z = {}
    oz = out["zariadenie"]
    for k in ("model", "nazov", "chladivo"):
        if k in z:
            oz[k] = str(z[k] or "").strip()[:80]
    if "typ" in z:
        if z["typ"] in TYPY_JEDNOTKY:
            oz["typ"] = z["typ"]
        else:
            err.append("zariadenie.typ: povolené %s." % ", ".join(TYPY_JEDNOTKY))
    if "kurenie" in z:
        if z["kurenie"] in TYPY_KURENIA:
            oz["kurenie"] = z["kurenie"]
        else:
            err.append("zariadenie.kurenie: povolené %s." % ", ".join(TYPY_KURENIA))
    if "vykon_kw" in z:
        x = _num(z["vykon_kw"], 2, 30)
        if x is None:
            err.append("zariadenie.vykon_kw: číslo 2–30.")
        else:
            oz["vykon_kw"] = x
    if "fazy" in z:
        if z["fazy"] in (1, 3, "1", "3"):
            oz["fazy"] = int(z["fazy"])
        else:
            err.append("zariadenie.fazy: 1 alebo 3.")
    if "tuv" in z:
        oz["tuv"] = bool(z["tuv"])
    if oz["model"] and not re.fullmatch(r"[A-Za-z0-9_\-.]{1,40}", oz["model"]):
        err.append("zariadenie.model: len písmená, číslice, - _ .")

    e = p.get("esp") or {}
    if isinstance(e, dict):
        for sec, keys in (("entity", DEVICE_KEYS), ("nastavenia", SETTING_KEYS)):
            src = e.get(sec)
            if src is None:
                continue
            if not isinstance(src, dict):
                err.append("esp.%s: musí byť objekt." % sec)
                continue
            for k, v in src.items():
                if k not in keys:
                    err.append("esp.%s: neznámy kľúč %s." % (sec, k))
                elif v in (None, ""):
                    out["esp"][sec][k] = ""          # prázdne = entita nie je k dispozícii
                elif isinstance(v, str) and len(v) <= 120:
                    out["esp"][sec][k] = v.strip()
                else:
                    err.append("esp.%s.%s: text do 120 znakov." % (sec, k))
        if "rest_extra" in e:
            out["esp"]["rest_extra"] = _strlist(e["rest_extra"])[:30]
    else:
        err.append("esp: musí byť objekt.")

    if "izby" in p:
        rooms = p["izby"]
        if not isinstance(rooms, list) or not rooms:
            err.append("izby: aspoň jedna izba.")
        else:
            seen, outr = set(), []
            for i, r in enumerate(rooms):
                if not isinstance(r, dict):
                    err.append("izby[%d]: musí byť objekt." % i)
                    continue
                k = str(r.get("kluc") or "").strip()
                if not KEY_RE.match(k) or k in RESERVED or k in seen:
                    err.append("izby[%d].kluc: malé písmená bez diakritiky (a-z, 0-9, _), jedinečný, nie rezervovaný (%s)." % (i, k))
                    continue
                seen.add(k)
                mn = r.get("min")
                mnv = None if mn in (None, "") else _num(mn, *ABS_ROOM_TARGET)
                if mn not in (None, "") and mnv is None:
                    err.append("izby[%d].min: %g–%g °C alebo prázdne." % ((i,) + ABS_ROOM_TARGET))
                outr.append({"kluc": k, "nazov": str(r.get("nazov") or k).strip()[:40], "esp": str(r.get("esp") or "").strip()[:120],
                             "referencna": bool(r.get("referencna")), "min": mnv, "ha": _strlist(r.get("ha"))[:5]})
            refs = [r for r in outr if r.get("referencna")]
            if outr and len(refs) != 1:
                err.append("izby: práve jedna izba musí byť referenčná (podľa nej firmvér posúva krivku).")
                if not refs:
                    outr[0]["referencna"] = True
                else:
                    for r in refs[1:]:
                        r["referencna"] = False
            if outr:
                out["izby"] = outr

    for sec in ("ine_teploty", "ha"):
        if sec in p:
            src = p[sec]
            if not isinstance(src, dict):
                err.append("%s: musí byť objekt." % sec)
                continue
            m = {}
            for k, v in src.items():
                if not KEY_RE.match(str(k)):
                    err.append("%s: neplatný kľúč %s." % (sec, k))
                    continue
                m[k] = _strlist(v)[:5]
            out[sec] = m
    room_set = {r["kluc"] for r in out["izby"]}
    for k in out["ine_teploty"]:
        if k in room_set or (k in RESERVED and k not in ("chodba", "kotolna")):
            err.append("ine_teploty.%s: koliduje s izbou alebo rezervovaným názvom." % k)

    mt = p.get("mantinely") or {}
    if not isinstance(mt, dict):
        err.append("mantinely: musí byť objekt.")
        mt = {}
    om = {"ciel_izby": list(d["mantinely"]["ciel_izby"])}
    for k, v in mt.items():
        if k == "ciel_izby":
            lo, hi = (_num(x, *ABS_ROOM_TARGET) for x in (v if isinstance(v, (list, tuple)) and len(v) == 2 else (None, None)))
            if lo is None or hi is None or lo >= hi:
                err.append("mantinely.ciel_izby: [min, max] v rozsahu %g–%g °C." % ABS_ROOM_TARGET)
            else:
                om["ciel_izby"] = [lo, hi]
        elif k in ABS_LIMITS:
            ok = isinstance(v, (list, tuple)) and len(v) == 5
            vals = [_num(x) for x in v] if ok else []
            if not ok or None in vals:
                err.append("mantinely.%s: [min, max, max_krok, hodín, zaokrúhlenie]." % k)
                continue
            lo, hi, step, hours, rnd = vals
            alo, ahi = ABS_LIMITS[k]
            if lo < alo or hi > ahi or lo >= hi or step <= 0 or hours < 1 or rnd <= 0:
                err.append("mantinely.%s: min/max v rozsahu %g–%g, min < max, krok a zaokrúhlenie > 0, hodín ≥ 1." % (k, alo, ahi))
                continue
            om[k] = [lo, hi, step, int(hours), rnd]
        else:
            err.append("mantinely: neznámy kľúč %s." % k)
    out["mantinely"] = om

    el = p.get("elektrina") or {}
    if isinstance(el, dict):
        if "ims_stat" in el:
            out["elektrina"]["ims_stat"] = str(el.get("ims_stat") or "").strip()[:120]
        if "meter_ids" in el:
            out["elektrina"]["meter_ids"] = _strlist(el.get("meter_ids"))[:4]
    return out, err


# ---------------------------------------------------------------- načítanie / uloženie
_cache = {}


def load(path=None):
    path = path or PROFILE_FILE
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return default()
    c = _cache.get(path)
    if c and c[0] == key:
        return copy.deepcopy(c[1])
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return default()          # poškodený súbor: radšej pôvodné správanie než pád služieb
    p, _ = validate(raw)
    _cache[path] = (key, p)
    return copy.deepcopy(p)


def errors(path=None):
    """Chyby uloženého profilu (pre web); chýbajúci súbor = žiadne chyby."""
    path = path or PROFILE_FILE
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return validate(json.load(f))[1]
    except (OSError, ValueError) as ex:
        return ["profil.json sa nedá prečítať: %s" % ex]


def save(p, path=None):
    path = path or PROFILE_FILE
    norm, err = validate(p)
    if err:
        return err
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(norm, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    _cache.pop(path, None)
    return []


# ---------------------------------------------------------------- odvodené mapy (kompatibilné s verziou 1.x)
def _p(p):
    return p if p is not None else load()


def reference_room(p=None):
    p = _p(p)
    for r in p["izby"]:
        if r.get("referencna"):
            return r["kluc"]
    return p["izby"][0]["kluc"]


def room_keys(p=None):
    """Kľúče izieb, referenčná prvá (ako pôvodné common.ROOMS)."""
    p = _p(p)
    ref = reference_room(p)
    return (ref,) + tuple(r["kluc"] for r in p["izby"] if r["kluc"] != ref)


def room_names(p=None):
    p = _p(p)
    return {r["kluc"]: r["nazov"] for r in p["izby"]}


def target_rooms(p=None):
    """Izby s cieľovým minimom (bez referenčnej), v poradí profilu – ako pôvodné common.TARGET_ROOMS."""
    p = _p(p)
    return tuple(r["kluc"] for r in p["izby"] if not r.get("referencna"))


def target_defaults(p=None):
    p = _p(p)
    return {r["kluc"]: {"min": r["min"] if r["min"] is not None else 21.0} for r in p["izby"] if not r.get("referencna")}


def esp_s(p=None):
    """Mapa ako pôvodné common.S."""
    p = _p(p)
    e = p["esp"]["entity"]
    ref = reference_room(p)
    m = {"vonku": e["vonku"], ref: e["sledovana"], "ciel": e["ciel_akt"], "posun": e["posun"], "wl": e["wl"], "tw2": e["tw2"],
         "tw1": e["tw1"], "dt": e["dt"], "freq": e["freq"], "prikon": e["prikon"], "bojler": e["tuv"], "pwm": e["pwm"]}
    for r in p["izby"]:
        if r["esp"]:
            m[r["kluc"] + "_z" if r["kluc"] == ref else r["kluc"]] = r["esp"]
    m.update({"chyba": e["chyba"], "spotreba": e["kwh_den"]})
    return {k: v for k, v in m.items() if v}


def esp_tc(p=None):
    """Mapa ako pôvodné stats.TC."""
    p = _p(p)
    e = p["esp"]["entity"]
    ref = reference_room(p)
    m = {"vonku": e["vonku"], "ciel": e["ciel_akt"], ref: e["sledovana"]}
    for r in p["izby"]:
        if r["kluc"] != ref and r["esp"]:
            m[r["kluc"]] = r["esp"]
    m.update({"tw2": e["tw2"], "tw1": e["tw1"], "wl": e["wl"], "dt": e["dt"], "tuv": e["tuv"], "tuv_ciel": e["tuv_ciel"],
              "freq": e["freq"], "kwh": e["kwh_den"], "prikon": e["prikon"], "chyba": e["chyba"], "flow": e["flow"]})
    return {k: v for k, v in m.items() if v}


def device_entity(key, p=None):
    """Názov entity zariadenia (DEVICE_KEYS) alebo '' ak nie je k dispozícii."""
    return _p(p)["esp"]["entity"].get(key) or ""


def settings_map(p=None):
    """Ako pôvodné common.SETTINGS (5 nastavení, ktoré sleduje denný súhrn)."""
    n = _p(p)["esp"]["nastavenia"]
    m = {"fsv2031": n["fsv2031"], "fsv2032": n["fsv2032"], "fsv4013": n["fsv4013"], "max_posun": n["max_posun"],
         "ciel_izba": _p(p)["esp"]["entity"]["ciel_izby"]}
    return {k: v for k, v in m.items() if v}


def setting_entity(key, p=None):
    return _p(p)["esp"]["nastavenia"].get(key) or ""


def limits(p=None):
    """Mantinely agenta: {názov entity: (min, max, max_krok, hodín, zaokrúhlenie)} – ako pôvodné agent.ALLOWED."""
    p = _p(p)
    preset = PRESETS[p["zariadenie"]["kurenie"]]
    out = {}
    for k in ("fsv2031", "fsv2032", "fsv4013", "max_posun"):
        ent = p["esp"]["nastavenia"].get(k)
        if not ent:
            continue
        v = p["mantinely"].get(k) or preset[k]
        lo, hi = max(v[0], ABS_LIMITS[k][0]), min(v[1], ABS_LIMITS[k][1])
        out[ent] = (float(lo), float(hi), float(v[2]), int(v[3]), float(v[4]))
    return out


def room_target_range(p=None):
    lo, hi = _p(p)["mantinely"]["ciel_izby"]
    return max(lo, ABS_ROOM_TARGET[0]), min(hi, ABS_ROOM_TARGET[1])


def ha_columns(p=None):
    """[(stĺpec, [HA entity_id…])] pre import histórie – ako pôvodné stats_backfill.COLUMN_IDS (poradie: vonku, izby, ostatné)."""
    p = _p(p)
    out = []
    if p["ha"].get("vonku"):
        out.append(("vonku", p["ha"]["vonku"]))
    ref = reference_room(p)
    for k in room_keys(p):
        r = next(x for x in p["izby"] if x["kluc"] == k)
        if r["ha"]:
            out.append((k, r["ha"]))
    for k, ids in p["ine_teploty"].items():
        if ids:
            out.append((k, ids))
    for k in ("tw2", "tw1", "wl", "tuv"):
        if p["ha"].get(k):
            out.append((k, p["ha"][k]))
    return out


def extra_temp_keys(p=None):
    return tuple(_p(p)["ine_teploty"])


# ---------------------------------------------------------------- model zariadenia (tabuľka výrobcu)
def list_models():
    """[{id, nazov, typ, vykon_kw, chladivo, overene}] zo súborov modely/*.json."""
    out = []
    try:
        names = sorted(os.listdir(MODELS_DIR))
    except OSError:
        return out
    for fn in names:
        if fn.endswith(".json"):
            m = _read_model(fn[:-5])
            if m:
                out.append({k: m.get(k) for k in ("id", "nazov", "typ", "vykon_kw", "chladivo", "overene")})
    return out


def _read_model(mid):
    if not re.fullmatch(r"[A-Za-z0-9_\-.]{1,40}", mid or ""):
        return None
    try:
        with open(os.path.join(MODELS_DIR, mid + ".json"), encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        return None
    ta, lw, peak = m.get("ta"), m.get("lw"), m.get("peak")
    if not (isinstance(ta, list) and isinstance(lw, list) and isinstance(peak, dict) and len(ta) >= 2 and len(lw) >= 2):
        return None
    tab = {}
    for t in ta:
        row = peak.get(str(t))
        if not isinstance(row, list) or len(row) != len(lw):
            return None
        tab[t] = tuple(None if c is None else (float(c[0]), float(c[1])) for c in row)
    if any(tab[t][0] is None for t in ta):
        return None                # prvý stĺpec (najnižšia voda) musí byť vyplnený
    m["id"] = mid
    m["tab"] = tab
    return m


def model(p=None):
    """Tabuľka výrobcu pre model v profile: {"id","nazov","ta":[…],"lw":[…],"tab":{ta:((HC,PI)|None,…)},"zdroj","overene"}
    alebo None (model neznámy – COP sa počíta bez tabuľky, prietok len zo senzora / konfigurácie)."""
    return _read_model(_p(p)["zariadenie"].get("model") or "")
