#!/usr/bin/env python3
"""Overenie JWT z Cloudflare Access (hlavička Cf-Access-Jwt-Assertion / cookie CF_Authorization), len stdlib.

verify(token, team_domain, aud, now=None, fetch=None) -> dict claims (email je lowercase) alebo vyhodí JwtError.
Povolené je iba RS256 + typ JWT. Podpis sa overuje čistým Pythonom (PKCS#1 v1.5, SHA-256) voči verejným kľúčom z
https://{team_domain}/cdn-cgi/access/certs. Fail-closed: bez team_domain alebo aud sa nič neoveruje (JwtError).
Token ani podpis sa nikdy nelogujú ani nevkladajú do textov chýb.
"""
import base64, hashlib, hmac, json, re, threading, time, urllib.request

from cftunnel import EMAIL_RE

JWKS_TTL = 3600          # cache kľúčov v pamäti (s)
JWKS_MIN_REFRESH = 60    # najčastejšie vynútené obnovenie pri neznámom kid (s)
FETCH_TIMEOUT = 10
MAX_TOKEN = 8192
LEEWAY_EXP = 30          # tolerancia hodín pri exp
LEEWAY_FUTURE = 60       # tolerancia hodín pri nbf / iat
TEAM_RE = r"[a-z0-9-]+\.cloudflareaccess\.com"
AUD_RE = r"[A-Za-z0-9]{32,128}"
# DER DigestInfo pre SHA-256 (RFC 8017, EMSA-PKCS1-v1_5)
SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
B64_RE = re.compile(r"[A-Za-z0-9_-]+")

JWKS_FAIL_BACKOFF = 15   # po zlyhanom stiahnutí kľúčov sa ďalších 15 s nepokúšame (s)
_fail = {}               # team_domain -> čas posledného zlyhania stiahnutia
_cache = {}            # team_domain -> {"keys": {kid: (n, e)}, "ts": float (načítané), "forced": float (posledné vynútené)}
_lock = threading.Lock()


class JwtError(Exception):
    pass


def _b64(s, what):
    if not isinstance(s, str) or not B64_RE.fullmatch(s):
        raise JwtError("Neplatný token (%s)." % what)
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except Exception:
        raise JwtError("Neplatný token (%s)." % what)


def _json(raw, what):
    try:
        v = json.loads(raw.decode("utf-8"))
    except Exception:
        raise JwtError("Neplatný token (%s)." % what)
    if not isinstance(v, dict):
        raise JwtError("Neplatný token (%s)." % what)
    return v


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) < 1e12


def _default_fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "kurenie-agent", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
        raw = r.read(1 << 20)
    return json.loads(raw.decode("utf-8"))


def _parse_keys(doc):
    """JWKS dokument -> {kid: (n, e)}; nepoužiteľné kľúče sa preskočia."""
    out = {}
    for k in (doc.get("keys") or []) if isinstance(doc, dict) else []:
        try:
            if not isinstance(k, dict) or k.get("kty") != "RSA" or not isinstance(k.get("kid"), str):
                continue
            if k.get("use") not in (None, "sig"):
                continue
            n = int.from_bytes(_b64(k["n"], "jwk"), "big")
            e = int.from_bytes(_b64(k["e"], "jwk"), "big")
        except (JwtError, KeyError):
            continue
        if n.bit_length() >= 2048 and 3 <= e < (1 << 64) and e % 2 == 1:
            out[k["kid"]] = (n, e)
    return out


def _load(team_domain, fetch):
    try:
        doc = (fetch or _default_fetch)("https://%s/cdn-cgi/access/certs" % team_domain)
    except Exception:
        raise JwtError("Verejné kľúče Cloudflare sa nepodarilo stiahnuť.")
    keys = _parse_keys(doc)
    if not keys:
        raise JwtError("Cloudflare nevrátil žiadne použiteľné verejné kľúče.")
    return keys


def _get_key(team_domain, kid, fetch):
    """(n, e) pre kid. Cache 1 h; neznámy kid = jedno vynútené obnovenie (najviac raz za 60 s)."""
    if fetch is not None:   # test / vlastné sťahovanie: bez cache
        k = _load(team_domain, fetch).get(kid)
        if not k:
            raise JwtError("Neznámy kľúč podpisu.")
        return k
    now = time.time()
    with _lock:
        ent = _cache.get(team_domain)
        if not ent or now - ent["ts"] > JWKS_TTL:
            if now - _fail.get(team_domain, 0.0) < JWKS_FAIL_BACKOFF:   # nedávne zlyhanie sťahovania: nezdržuj každú požiadavku
                raise JwtError("Verejné kľúče Cloudflare sa nepodarilo stiahnuť.")
            try:
                keys = _load(team_domain, None)
            except JwtError:
                _fail[team_domain] = now
                raise
            _fail.pop(team_domain, None)
            ent = _cache[team_domain] = {"keys": keys, "ts": now, "forced": ent["forced"] if ent else 0.0}
        k = ent["keys"].get(kid)
        if k:
            return k
        if now - ent["forced"] >= JWKS_MIN_REFRESH:
            ent["forced"] = now
            try:
                ent["keys"] = _load(team_domain, None)
                ent["ts"] = now
            except JwtError:
                pass
            k = ent["keys"].get(kid)
            if k:
                return k
        raise JwtError("Neznámy kľúč podpisu.")


def _rs256_ok(signing_input, sig, n, e):
    k = (n.bit_length() + 7) // 8
    if len(sig) != k:
        return False
    s = int.from_bytes(sig, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    t = SHA256_PREFIX + hashlib.sha256(signing_input).digest()
    if k < len(t) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(em, expected)


def verify(token, team_domain, aud, now=None, fetch=None):
    if not isinstance(team_domain, str) or not re.fullmatch(TEAM_RE, team_domain) or not isinstance(aud, str) or not re.fullmatch(AUD_RE, aud):
        raise JwtError("Overovanie prihlásenia cez Cloudflare nie je nastavené.")
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN:
        raise JwtError("Neplatný token.")
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise JwtError("Neplatný token (štruktúra).")
    head = _json(_b64(parts[0], "hlavička"), "hlavička")
    if head.get("alg") != "RS256":
        raise JwtError("Nepovolený algoritmus podpisu.")
    typ = head.get("typ")   # Cloudflare Access hlavičku `typ` môže vynechať; ak je, musí to byť JWT
    if typ is not None and not (isinstance(typ, str) and typ.upper() == "JWT"):
        raise JwtError("Nepovolený typ tokenu.")
    kid = head.get("kid")
    if not isinstance(kid, str) or not kid or len(kid) > 200:
        raise JwtError("Token nemá platný kid.")
    sig = _b64(parts[2], "podpis")
    payload = _json(_b64(parts[1], "obsah"), "obsah")
    n, e = _get_key(team_domain, kid, fetch)
    if not _rs256_ok((parts[0] + "." + parts[1]).encode("ascii"), sig, n, e):
        raise JwtError("Podpis tokenu je neplatný.")
    # --- podpis sedí, teraz claims
    now = time.time() if now is None else now
    if payload.get("iss") != "https://" + team_domain:
        raise JwtError("Neplatný vydavateľ tokenu.")
    a = payload.get("aud")
    if isinstance(a, str):
        a = [a]
    if not isinstance(a, list) or not any(isinstance(x, str) and hmac.compare_digest(x.encode(), aud.encode()) for x in a):
        raise JwtError("Token nepatrí tejto aplikácii.")
    exp = payload.get("exp")
    if not _num(exp) or exp <= now - LEEWAY_EXP:
        raise JwtError("Token vypršal.")
    for k in ("nbf", "iat"):
        if k in payload:
            if not _num(payload[k]) or payload[k] > now + LEEWAY_FUTURE:
                raise JwtError("Token ešte nie je platný.")
    t = payload.get("type")
    if t is not None and t != "app":
        raise JwtError("Nepovolený druh tokenu.")
    em = payload.get("email")
    if not isinstance(em, str) or not em.strip():
        raise JwtError("Token neobsahuje e-mail.")
    em = em.strip().lower()
    if len(em) > 320 or not re.fullmatch(EMAIL_RE, em):
        raise JwtError("Token obsahuje neplatný e-mail.")
    out = dict(payload)
    out["email"] = em
    return out
