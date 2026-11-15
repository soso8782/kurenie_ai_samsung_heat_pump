#!/usr/bin/env python3
"""Automatické nastavenie Cloudflare tunela cez Cloudflare API (len stdlib).

setup(api_token, hostname, emails, no_access) vytvorí/znovu použije tunel „kurenie“, nastaví ingress hostname -> http://localhost:80,
vytvorí Access aplikáciu (povolené e-maily) a až potom DNS záznam. API token sa NEUKLADÁ ani nelogguje; vráti sa len token tunela
(ten sa uloží do súboru na serveri). Chyby: CfError s textom po slovensky (obsahuje správu z Cloudflare).
"""
import json, re, urllib.error, urllib.parse, urllib.request

API = "https://api.cloudflare.com/client/v4"
TUNNEL_NAME = "kurenie"
POLICY_NAME = "Povolené e-maily"
HOST_RE = r"(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}"
EMAIL_RE = r"[^@\s,;]{1,64}@[^@\s,;]{1,255}\.[^@\s,;]{2,63}"


class CfError(Exception):
    pass


def call(token, method, path, body=None, query=None):
    url = API + path + ("?" + urllib.parse.urlencode(query) if query else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "User-Agent": "kurenie-agent"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
    except Exception as e:
        raise CfError("Cloudflare neodpovedá: %s" % e)
    try:
        j = json.loads(raw)
    except ValueError:
        raise CfError("Cloudflare vrátil neplatnú odpoveď.")
    if not j.get("success"):
        msg = "; ".join("%s (kód %s)" % (x.get("message", "?"), x.get("code", "?")) for x in (j.get("errors") or [])) or "neznáma chyba"
        raise CfError("%s %s: %s" % (method, path.split("?")[0], msg))
    return j.get("result")


def setup(api_token, hostname, emails, no_access=False, log=None):
    log = log if log is not None else []
    hostname = (hostname or "").strip().lower().rstrip(".")
    api_token = (api_token or "").strip()
    if not re.fullmatch(HOST_RE, hostname):
        raise CfError("Neplatný hostname (napr. kurenie.tvojadomena.sk).")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{20,200}", api_token):
        raise CfError("API token nevyzerá správne.")
    emails = [e.strip().lower() for e in re.split(r"[\s,;]+", emails or "") if e.strip()]
    for e in emails:
        if not re.fullmatch(EMAIL_RE, e):
            raise CfError("Neplatný e-mail: %s" % e)
    if not emails and not no_access:
        raise CfError("Zadaj aspoň jeden povolený e-mail (Cloudflare Access), alebo výslovne potvrď prevádzku bez ochrany.")

    call(api_token, "GET", "/user/tokens/verify")
    log.append("API token je platný")

    # zóna: skús postupne kratšie prípony hostname
    parts = hostname.split(".")
    zone = None
    for i in range(len(parts) - 1):
        name = ".".join(parts[i:])
        res = call(api_token, "GET", "/zones", query={"name": name})
        if res:
            zone = res[0]
            break
    if not zone:
        raise CfError("Doména pre %s sa v tomto Cloudflare účte nenašla (token musí mať právo čítať zónu)." % hostname)
    zone_id = zone["id"]
    account_id = (zone.get("account") or {}).get("id")
    if not account_id:
        raise CfError("Nepodarilo sa zistiť Cloudflare účet.")
    log.append("zóna %s nájdená" % zone["name"])

    # tunel
    tunnels = call(api_token, "GET", "/accounts/%s/cfd_tunnel" % account_id, query={"name": TUNNEL_NAME, "is_deleted": "false"})
    if tunnels:
        tid = tunnels[0]["id"]
        log.append("použitý existujúci tunel „%s“" % TUNNEL_NAME)
    else:
        import base64, os
        t = call(api_token, "POST", "/accounts/%s/cfd_tunnel" % account_id,
                 {"name": TUNNEL_NAME, "config_src": "cloudflare", "tunnel_secret": base64.b64encode(os.urandom(32)).decode()})
        tid = t["id"]
        log.append("vytvorený tunel „%s“" % TUNNEL_NAME)
    call(api_token, "PUT", "/accounts/%s/cfd_tunnel/%s/configurations" % (account_id, tid),
         {"config": {"ingress": [{"hostname": hostname, "service": "http://localhost:80"}, {"service": "http_status:404"}]}})
    log.append("ingress %s → http://localhost:80" % hostname)
    tunnel_token = call(api_token, "GET", "/accounts/%s/cfd_tunnel/%s/token" % (account_id, tid))
    if not isinstance(tunnel_token, str) or len(tunnel_token) < 40:
        raise CfError("Nepodarilo sa získať token tunela.")

    # Access PRED DNS: ak zlyhá, stránka sa nezverejní bez ochrany
    app_id, aud, team_domain = None, None, None
    if emails:
        apps = call(api_token, "GET", "/accounts/%s/access/apps" % account_id) or []
        found = [a for a in apps if (a.get("domain") or "").lower() == hostname]
        if found:
            app_id = found[0].get("id")
            aud = found[0].get("aud")
            log.append("Cloudflare Access aplikácia pre %s už existuje – ponechaná (skontroluj jej pravidlá)" % hostname)
        else:
            created = call(api_token, "POST", "/accounts/%s/access/apps" % account_id,
                           {"name": "Kúrenie", "domain": hostname, "type": "self_hosted", "session_duration": "24h",
                            "policies": [{"name": POLICY_NAME, "decision": "allow", "include": [{"email": {"email": e}} for e in emails]}]})
            app_id = (created or {}).get("id")
            aud = (created or {}).get("aud")
            log.append("Cloudflare Access: povolené %s" % ", ".join(emails))
        # údaje na overovanie prihlásenia (JWT); ak sa nepodarí zistiť, setup nezlyhá
        try:
            org = call(api_token, "GET", "/accounts/%s/access/organizations" % account_id) or {}
            team_domain = str(org.get("auth_domain") or "").strip().lower() or None
        except CfError:
            team_domain = None
        if not (isinstance(aud, str) and re.fullmatch(r"[A-Za-z0-9]{32,128}", aud)):
            aud = None
        if not (team_domain and re.fullmatch(r"[a-z0-9-]+\.cloudflareaccess\.com", team_domain)):
            team_domain = None
        if aud and team_domain:
            log.append("Overovanie prihlásenia Cloudflare nastavené (%s)" % team_domain)
        else:
            log.append("Overovanie prihlásenia Cloudflare nie je nastavené – doplň ručne v Nastaveniach.")
    else:
        log.append("POZOR: bez Cloudflare Access")

    # DNS
    target = "%s.cfargotunnel.com" % tid
    rec = call(api_token, "GET", "/zones/%s/dns_records" % zone_id, query={"name": hostname})
    body = {"type": "CNAME", "name": hostname, "content": target, "proxied": True}
    if rec:
        call(api_token, "PUT", "/zones/%s/dns_records/%s" % (zone_id, rec[0]["id"]), body)
        log.append("DNS záznam aktualizovaný")
    else:
        call(api_token, "POST", "/zones/%s/dns_records" % zone_id, body)
        log.append("DNS záznam vytvorený")
    ids = {"account_id": account_id, "zone_id": zone_id, "tunnel_id": tid, "access_app_id": app_id, "aud": aud, "team_domain": team_domain}
    return tunnel_token, hostname, ids


def norm_email(e):
    """Lowercase e-mail alebo ValueError po slovensky."""
    e = str(e or "").strip().lower()
    if not re.fullmatch(EMAIL_RE, e):
        raise ValueError("Neplatný e-mail.")
    return e


def verify_token(api_token):
    """Over API token (GET /user/tokens/verify). CfError ak nie je platný."""
    api_token = (api_token or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{20,200}", api_token):
        raise CfError("API token nevyzerá správne.")
    call(api_token, "GET", "/user/tokens/verify")
    return True


_POLICY_KEYS = ("name", "decision", "exclude", "require", "session_duration", "purpose_justification_required",
                "purpose_justification_prompt", "approval_required", "approval_groups", "isolation_required")


def sync_emails(api_token, account_id, app_id, emails):
    """Nastaví povolené e-maily v allow politike Access aplikácie (include = zoznam e-mailov). CfError pri zlyhaní."""
    if not (api_token and account_id and app_id):
        raise CfError("Chýba API token alebo identifikátory Cloudflare Access.")
    emails = [norm_email(e) for e in emails]
    if not emails:
        raise CfError("Zoznam e-mailov je prázdny.")
    include = [{"email": {"email": e}} for e in emails]
    base = "/accounts/%s/access" % account_id
    app = call(api_token, "GET", "%s/apps/%s" % (base, app_id)) or {}
    pols = [p for p in (app.get("policies") or []) if isinstance(p, dict)]
    allow = [p for p in pols if p.get("decision") == "allow"]
    pick = next((p for p in allow if p.get("name") == POLICY_NAME), allow[0] if allow else None)
    if pick is None:
        call(api_token, "POST", "%s/apps/%s/policies" % (base, app_id),
             {"name": POLICY_NAME, "decision": "allow", "include": include})
        return "Access: vytvorené pravidlo „%s“ (%d e-mailov)" % (POLICY_NAME, len(emails))
    body = {k: pick[k] for k in _POLICY_KEYS if k in pick}
    body["include"] = include
    pid = pick.get("id")
    attempts = []
    if pid:
        app_level = ("PUT", "%s/apps/%s/policies/%s" % (base, app_id, pid))
        reusable = ("PUT", "%s/policies/%s" % (base, pid))
        attempts = [reusable, app_level] if pick.get("reusable") else [app_level, reusable]
    last = None
    for method, path in attempts:
        try:
            call(api_token, method, path, body)
            return "Access: povolených %d e-mailov" % len(emails)
        except CfError as e:
            last = e
    # posledná možnosť: celá aplikácia s upravenými politikami
    try:
        newpols = [dict(p, include=include) if p is pick else p for p in pols]
        appbody = {k: v for k, v in app.items() if k not in ("id", "uid", "aud", "created_at", "updated_at")}
        appbody["policies"] = newpols
        call(api_token, "PUT", "%s/apps/%s" % (base, app_id), appbody)
        return "Access: povolených %d e-mailov" % len(emails)
    except CfError as e:
        raise last or e
