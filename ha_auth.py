#!/usr/bin/env python3
"""Ochrana pred zahlcovaním Home Assistanta neplatným tokenom.

Keď HA odmietne token (HTTP 401/403 alebo WebSocket auth_invalid), ďalšie automatické pokusy s tou istou dvojicou
(adresa, token) sa na BACKOFF_S sekúnd preskočia bez sieťovej požiadavky. HA si každý neplatný pokus zapisuje do logu
a pri zapnutom `ip_ban_enabled` by adresu agenta zablokoval. Nový token (alebo adresa) = iný kľúč = okamžitý pokus.
Ručný test z webu (`force=True`) obmedzenie obchádza. Stav je v pamäti procesu (agent a web majú každý svoj).
Iba štandardná knižnica; token sa nikde neukladá ani nevypisuje (len jeho SHA-256 odtlačok v pamäti).
"""
import hashlib, threading, time
from i18n import tr

BACKOFF_S = 900          # 15 min medzi automatickými pokusmi s odmietnutým tokenom
_lock = threading.Lock()
_blocked = {}            # odtlačok -> čas, do ktorého sa nepokúšať


def _key(url, token):
    return hashlib.sha256(("%s\n%s" % ((url or "").strip().rstrip("/"), (token or "").strip())).encode("utf-8")).hexdigest()


def blocked_for(url, token, now=None):
    """Koľko sekúnd ešte platí blokovanie (0 = môže sa skúsiť)."""
    now = time.time() if now is None else now
    with _lock:
        until = _blocked.get(_key(url, token), 0)
    return max(0, int(until - now))


def failed(url, token, now=None):
    now = time.time() if now is None else now
    with _lock:
        _blocked[_key(url, token)] = now + BACKOFF_S


def ok(url, token):
    with _lock:
        _blocked.pop(_key(url, token), None)


def message(code=None, lang="sk"):
    """Zrozumiteľná hláška pre používateľa pri odmietnutom tokene."""
    what = (" (HTTP %d)" % code) if code else ""
    return tr("srv.ha_auth.odmietol", lang, what=what)


def check(url, token, force=False, lang="sk"):
    """Pred požiadavkou: ak je dvojica blokovaná a nejde o ručný test, vyhodí RuntimeError bez kontaktu s HA."""
    if force:
        return
    left = blocked_for(url, token)
    if left:
        raise RuntimeError(message(lang=lang) + " " + tr("srv.ha_auth.dalsi_pokus", lang, n=max(1, (left + 59) // 60)))
