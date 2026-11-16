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
