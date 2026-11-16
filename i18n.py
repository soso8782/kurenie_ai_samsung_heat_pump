#!/usr/bin/env python3
"""Preklad textov zo servera (SPEC_server_i18n): kľúče `srv.*` z locales/<jazyk>.json (tie isté súbory ako web).

tr(key, lang, **vars)     text; chýbajúci kľúč -> slovenčina -> samotný kľúč; {nazov} sa nahradí hodnotou
tn(key, n, lang, **vars)  množné číslo (sk: .1 / .2_4 / .5, en: .1 / .n), {n} = n
req_lang(headers)         jazyk požiadavky z hlavičky X-Lang (len známe jazyky z locales/), inak sk
home_lang(cfg)            jazyk domácnosti (UI_LANG v config.env), inak sk
Súbory sa načítajú raz a znova až pri zmene mtime."""
import json
import os
import re
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
LOC = os.path.join(ROOT, "locales")
DEFAULT = "sk"
LANG_NAMES = {"sk": "slovenčina", "en": "English"}     # pre pokyn jazyka v promptoch agenta
_LANG_RE = re.compile(r"^[a-z]{2}$")
_VAR_RE = re.compile(r"\{(\w+)\}")
_lock = threading.Lock()
_cache = {}     # jazyk -> (mtime, dict)


def known_langs():
    """jazyky, ktoré majú súbor locales/xx.json"""
    try:
        return sorted(f[:-5] for f in os.listdir(LOC) if f.endswith(".json") and _LANG_RE.match(f[:-5]))
    except OSError:
        return [DEFAULT]


def _load(lang):
    if not isinstance(lang, str) or not _LANG_RE.match(lang):
        return {}
    path = os.path.join(LOC, lang + ".json")
    try:
        mt = os.stat(path).st_mtime
    except OSError:
        return {}
    with _lock:
        hit = _cache.get(lang)
        if hit and hit[0] == mt:
            return hit[1]
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict):
                d = {}
        except (OSError, ValueError):
            d = {}
        _cache[lang] = (mt, d)
        return d


def _sub(text, vars):
    if not vars:
        return text
    return _VAR_RE.sub(lambda m: str(vars[m.group(1)]) if m.group(1) in vars else m.group(0), text)


def _lookup(key, lang):
    for lg in (lang, DEFAULT):
        v = _load(lg).get(key)
        if isinstance(v, str):
            return v
    return None


def tr(key, lang=DEFAULT, **vars):
    v = _lookup(key, lang)
    return _sub(key if v is None else v, vars)


def _plural_suffix(n, lang):
    if lang == "sk" or lang == "cs":
        return ".1" if n == 1 else (".2_4" if n in (2, 3, 4) else ".5")
    return ".1" if n == 1 else ".n"


def tn(key, n, lang=DEFAULT, **vars):
    vars["n"] = n
    suf = _plural_suffix(n, lang)
    v = _lookup(key + suf, lang)
    if v is None:                       # jazyk bez takej prípony -> slovenské množné číslo
        v = _lookup(key + _plural_suffix(n, DEFAULT), DEFAULT)
    return _sub(key + suf if v is None else v, vars)


def req_lang(headers):
    try:
        v = (headers.get("X-Lang") or "").strip().lower()[:2]
    except Exception:
        v = ""
    return v if v and v in known_langs() else DEFAULT


def home_lang(cfg):
    try:
        v = str((cfg or {}).get("UI_LANG") or "").strip().lower()[:2]
    except Exception:
        v = ""
    return v if v and v in known_langs() else DEFAULT


def dec(num_text, lang=DEFAULT):
    """číslo zapísané slovenskou desatinnou čiarkou ("3,5", "−2,0") -> desatinný oddeľovač jazyka (en: bodka); sk bez zmeny"""
    return num_text if lang == "sk" else str(num_text).replace(",", ".")


def lang_name(lang):
    """názov jazyka pre pokyn v prompte agenta"""
    return LANG_NAMES.get(lang) or lang


# ---------------------------------------------------------------- texty udalostí zberača (tc.db, tabuľka events)
# Zberač ukladá nové udalosti v jazyku domácnosti (UI_LANG); staré slovenské riadky sa pri zobrazení / pre model prekladajú
# (aj opačne, ak sa UI_LANG zmenil). START/STOP na začiatku ostáva v oboch jazykoch (SQL `text LIKE 'STOP%'`).
_EV_FIXED = (("Zberač spustený", "Collector started"), ("ESP znova dostupné", "ESP reachable again"),
             ("Nenulový chybový kód TČ", "Non-zero heat pump error code"), ("Zber ukončený (koniec 48 h)", "Collection ended (48 h limit)"))
_EV_CTX = (("vonku ", "outdoor "), (", ventil ", ", valve "), (", termostat ", ", thermostat "), (", odmraz. ", ", defrost "))
_RE_ESP_SK = re.compile(r"^ESP nedostupné \((.*)\)$")
_RE_ESP_EN = re.compile(r"^ESP unreachable \((.*)\)$")
_RE_START_SK = re.compile(r"^START \(pauza (\d+) min\) - (.*)$", re.S)
_RE_START_EN = re.compile(r"^START \(pause (\d+) min\) - (.*)$", re.S)
_RE_STOP_SK = re.compile(r"^STOP \(chod (\d+) min, max (\d+) Hz\) - (.*)$", re.S)
_RE_STOP_EN = re.compile(r"^STOP \(ran (\d+) min, max (\d+) Hz\) - (.*)$", re.S)


_EV_ENT = (("zberač", "collector"), ("Kompresor", "Compressor"))


def event_entity(name, lang=DEFAULT):
    """Názov „entity“ udalosti zo zberača (zberač, Kompresor) v jazyku `lang`; ostatné (názvy z ESP) sa nemení."""
    if lang == "en":
        return dict(_EV_ENT).get(name, name)
    if lang == "sk":
        return {b: a for a, b in _EV_ENT}.get(name, name)
    return name


def _ctx(t, to_en):
    for a, b in _EV_CTX:
        t = t.replace(a, b) if to_en else t.replace(b, a)
    return t


def event_text(text, lang=DEFAULT):
    """Text udalosti zo zberača v jazyku `lang` (sk/en); neznámy text (logy z ESP, názvy) sa nemení."""
    t = text if isinstance(text, str) else ("" if text is None else str(text))
    if lang not in ("sk", "en"):
        return t
    en = lang == "en"
    for sk_, en_ in _EV_FIXED:
        if t == (sk_ if en else en_):
            return en_ if en else sk_
    m = (_RE_ESP_SK if en else _RE_ESP_EN).match(t)
    if m:
        return ("ESP unreachable (%s)" if en else "ESP nedostupné (%s)") % m.group(1)
    m = (_RE_START_SK if en else _RE_START_EN).match(t)
    if m:
        return ("START (pause %s min) - %s" if en else "START (pauza %s min) - %s") % (m.group(1), _ctx(m.group(2), en))
    m = (_RE_STOP_SK if en else _RE_STOP_EN).match(t)
    if m:
        return ("STOP (ran %s min, max %s Hz) - %s" if en else "STOP (chod %s min, max %s Hz) - %s") % (m.group(1), m.group(2), _ctx(m.group(3), en))
    return t
