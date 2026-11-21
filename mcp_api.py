#!/usr/bin/env python3
"""MCP server aplikácie (Model Context Protocol, transport Streamable HTTP, len JSON odpovede): POST /mcp.

Claude (Code, Desktop) sa cez neho pýta na stav kúrenia, dni, spotrebu, zásahy a behy agenta a môže agentovi poslať správu.
Nastavenia tepelného čerpadla MCP nemení – to robí len agent so svojimi mantinelmi.
Prístup: vlastný kľúč (`Authorization: Bearer <kľúč>`), v databáze agenta je len jeho SHA-256 (rovnako ako kľúč integrácie HA).
Kľúč vytvorí správca na webe (Nastavenia → MCP server pre Claude) alebo na serveri: `python3 mcp_api.py --novy-kluc`.
Napojenie na HTTP server robí app.py (modul app sa odovzdáva ako parameter, bez cyklického importu).
"""
import hashlib, hmac, json, secrets, sys, time

PATH = "/mcp"
KEY_HASH = "mcp_api_key_hash"
KEY_CREATED = "mcp_api_key_created"
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "samsung-heat-pump-ai-control", "title": "Samsung Heat Pump AI Control"}
MAX_TEXT = 200_000          # znakov výstupu nástroja (väčší výstup sa skráti s poznámkou)
DAY_EVENTS_MAX = 300
INSTRUCTIONS = ("Domáce kúrenie s tepelným čerpadlom Samsung EHS a AI agentom. Teploty sú v °C, časy (ts) v sekundách Unix, "
                "dátumy YYYY-MM-DD v miestnom čase domácnosti. Začni nástrojom `stav`. Nastavenia čerpadla tento server nemení; "
                "požiadavku na zmenu pošli agentovi nástrojom `poslat_spravu` (agent ju posúdi v rámci svojich mantinelov).")


# ---------------------------------------------------------------- kľúč
def _get(c, k):
    r = c.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
    return r[0] if r else None


def _set(c, k, v):
    c.execute("INSERT OR REPLACE INTO state(k, v) VALUES(?, ?)", (k, str(v)))
    c.commit()


def _hash(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def new_key(c, now=None):
    """Nový kľúč (starý prestane platiť); uloží sa len hash, kľúč sa zobrazí raz."""
    key = "kmcp_" + secrets.token_urlsafe(32)
    _set(c, KEY_HASH, _hash(key))
    _set(c, KEY_CREATED, int(now if now is not None else time.time()))
    return key


def revoke(c):
    c.execute("DELETE FROM state WHERE k IN (?, ?)", (KEY_HASH, KEY_CREATED))
    c.commit()


def key_info(c):
    """Pre kartu v nastaveniach: {"set", "created", "path"} (nikdy nie samotný kľúč ani hash)."""
    h = _get(c, KEY_HASH)
    try:
        created = int(_get(c, KEY_CREATED) or 0) or None
    except ValueError:
        created = None
    return {"set": bool(h), "created": created if h else None, "path": PATH}


def check(c, header):
    """True, ak hlavička `Authorization: Bearer <kľúč>` zodpovedá uloženému kľúču. Bez kľúča vždy False."""
    stored = _get(c, KEY_HASH)
    if not stored or not isinstance(header, str):
        return False
    h = header.strip()
    if h[:7].lower() != "bearer ":
        return False
    tok = h[7:].strip()
    return bool(tok) and hmac.compare_digest(_hash(tok), stored)


# ---------------------------------------------------------------- nástroje
def _date_arg(args, k):
    v = args.get(k)
    if v in (None, ""):
        return ""
    if not isinstance(v, str) or len(v) != 10 or v[4] != "-" or v[7] != "-":
        raise ValueError("%s: očakávaný dátum YYYY-MM-DD" % k)
    time.strptime(v, "%Y-%m-%d")
    return v


def _int_arg(args, k, default, lo, hi):
    v = args.get(k, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v):
        raise ValueError("%s: očakávané celé číslo" % k)
    return max(lo, min(hi, int(v)))


RUN_KEYS = ("id", "ts", "mode", "status", "stav", "summary", "advice", "actions", "error")


def t_stav(cfg, app, args):
    st = app.status(cfg, "admin", None, app.i18n.home_lang(cfg))
    keep = ("now", "mode", "running", "next_run", "interval", "current", "prevadzka", "alerts", "targets", "season",
            "windows_open", "dhw", "tuv", "collector_age", "live", "abtest", "runs_today", "cost_today", "dry_run")
    out = {k: st.get(k) for k in keep if k in st}
    lr = st.get("last_run")
    if lr:
        out["last_run"] = {k: lr.get(k) for k in RUN_KEYS}
    return out


def t_den(cfg, app, args):
    d = app.day(cfg, _date_arg(args, "datum"), app.i18n.home_lang(cfg))
    out = {k: d.get(k) for k in ("date", "summary", "hours", "day_review", "messages", "interventions", "okna", "notes")}
    out["runs"] = [{k: r.get(k) for k in RUN_KEYS} for r in d.get("runs") or []]
    if args.get("udalosti"):
        ev = [e for e in d.get("events") or [] if e.get("kind") != "log"]
        out["events"] = ev[-DAY_EVENTS_MAX:]
        out["events_vynechane"] = max(0, len(ev) - DAY_EVENTS_MAX)
    return out


def t_dni(cfg, app, args):
    n = _int_arg(args, "pocet", 14, 1, 60)
    h = app.history(cfg)
    return {"days": (h.get("days") or [])[:n], "lessons": h.get("lessons")}


def t_spotreba(cfg, app, args):
    return app.spotreba.report(cfg, _date_arg(args, "od") or None, _date_arg(args, "do") or None, lang=app.i18n.home_lang(cfg))


def t_zasahy(cfg, app, args):
    n = _int_arg(args, "pocet", 20, 1, 200)
    c = app.adb(cfg)
    try:
        return {"items": app.interventions.list_items(c, n)}
    finally:
        c.close()


def t_spravy(cfg, app, args):
    return {"messages": app.messages(cfg, limit=_int_arg(args, "pocet", 30, 1, 200))}


def t_beh_agenta(cfg, app, args):
    rid = _int_arg(args, "id", 0, 0, 10 ** 9)
    out = app.run_detail(cfg, rid)
    if not out:
        raise ValueError("beh %d neexistuje" % rid)
    return out


def t_poslat_spravu(cfg, app, args):
    text = args.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
        raise ValueError("text: 1–2000 znakov")
    c = app.adb(cfg)
    info = {}
    try:
        reply, cost, err = app.chat_reply(cfg, c, "[MCP] " + text.strip(), info=info, role="admin", email=None,
                                          lang=app.i18n.home_lang(cfg))
    finally:
        c.close()
    return {"reply": reply, "error": err, "cost": cost, "intervention": info.get("intervention")}


_DATE = {"type": "string", "description": "Dátum YYYY-MM-DD (miestny čas). Prázdne = dnes."}
_N = lambda d, mx: {"type": "integer", "minimum": 1, "maximum": mx, "description": "Počet (%d)." % d}
TOOLS = [
    ("stav", t_stav, True, "Aktuálny stav: teploty izieb a vody, čo práve robí čerpadlo (kúrenie/TÚV/odmrazovanie), režim agenta, "
     "posledný beh agenta, upozornenia, otvorené okná, ohrev TÚV.", {}),
    ("den", t_den, True, "Jeden deň: súhrn, tabuľka hodina po hodine (izby, vonku, voda, beh kompresora, kWh, TÚV), hodnotenie dňa, "
     "behy agenta, zásahy, správy, otvorené okná. Voliteľne aj udalosti (zmeny nastavení, štarty kompresora).",
     {"datum": _DATE, "udalosti": {"type": "boolean", "description": "Pridať zoznam udalostí (najviac 300 posledných)."}}),
    ("dni", t_dni, True, "Denné súhrny posledných dní (izby, vonku, kWh, kWh na 1 °C, beh, štarty, TÚV, nastavenia krivky) "
     "a aktuálne poučenia agenta.", {"pocet": _N(14, 60)}),
    ("spotreba", t_spotreba, True, "Spotreba elektriny domu a tepelného čerpadla po dňoch s odhadom ceny.",
     {"od": dict(_DATE, description="Od dátumu YYYY-MM-DD (prázdne = predvolené obdobie)."),
      "do": dict(_DATE, description="Do dátumu YYYY-MM-DD.")}),
    ("zasahy", t_zasahy, True, "Ručné zásahy (ventily, radiátory) a zmeny krivky s vyhodnotením pred/po (teploty izieb, "
     "kWh na 1 °C, Delta T).", {"pocet": _N(20, 200)}),
    ("spravy", t_spravy, True, "Posledné správy medzi majiteľom a agentom.", {"pocet": _N(30, 200)}),
    ("beh_agenta", t_beh_agenta, True, "Úplný vstup a odpoveď modelu pre jeden beh agenta (id z nástroja `stav` alebo `den`).",
     {"id": {"type": "integer", "minimum": 1, "description": "id behu"}}),
    ("poslat_spravu", t_poslat_spravu, False, "Pošle správu AI agentovi kúrenia (ako z webu) a vráti jeho odpoveď. Agent zo správy "
     "môže zapísať zásah (napr. priškrtenie ventilu) alebo posúdiť požiadavku; nastavenia čerpadla mení len v rámci svojich mantinelov. "
     "Stojí zlomok centa (volanie modelu).", {"text": {"type": "string", "minLength": 1, "maxLength": 2000}}),
]
_REQUIRED = {"beh_agenta": ["id"], "poslat_spravu": ["text"]}
_BY_NAME = {t[0]: t for t in TOOLS}


def tools_list():
    out = []
    for name, _, ro, desc, props in TOOLS:
        schema = {"type": "object", "properties": props, "additionalProperties": False}
        if name in _REQUIRED:
            schema["required"] = _REQUIRED[name]
        out.append({"name": name, "description": desc, "inputSchema": schema,
                    "annotations": {"readOnlyHint": ro, "destructiveHint": False, "openWorldHint": False}})
    return out


def _text(obj):
    s = json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":"))
    if len(s) > MAX_TEXT:
        s = s[:MAX_TEXT] + "\n…[výstup skrátený na %d znakov; zúž rozsah]" % MAX_TEXT
    return s


def _res(text, err):
    return {"content": [{"type": "text", "text": text}], "isError": err}


def call_tool(cfg, app, name, args):
    t = _BY_NAME.get(name)
    if not t:
        return None
    if not isinstance(args, dict):
        return _res("arguments musí byť objekt", True)
    unknown = set(args) - set(t[4])
    if unknown:
        return _res("neznáme parametre: %s" % ", ".join(sorted(unknown)), True)
    try:
        return _res(_text(t[1](cfg, app, args)), False)
    except ValueError as e:
        return _res("chybný vstup: %s" % e, True)
    except Exception as e:
        print("mcp: nástroj %s zlyhal: %s" % (name, type(e).__name__), file=sys.stderr, flush=True)
        return _res("chyba servera: %s" % type(e).__name__, True)


# ---------------------------------------------------------------- JSON-RPC
def _err(rid, code, msg):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}


def _ok(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def handle(cfg, body, app):
    """Jedna JSON-RPC správa -> (HTTP kód, telo | None). Notifikácie a odpovede klienta = 202 bez tela."""
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        return 400, _err(None, -32600, "Invalid Request")
    method, rid = body.get("method"), body.get("id")
    if method is None or "id" not in body:      # notifikácia (napr. notifications/initialized) alebo odpoveď klienta
        return 202, None
    params = body.get("params") or {}
    if not isinstance(params, dict):
        return 200, _err(rid, -32602, "Invalid params")
    if method == "initialize":
        want = params.get("protocolVersion")
        try:
            with open(app.os.path.join(app.ROOT, "VERSION"), encoding="utf-8") as f:
                v = f.read().strip()
        except Exception:
            v = "?"
        return 200, _ok(rid, {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                              "capabilities": {"tools": {"listChanged": False}},
                              "serverInfo": dict(SERVER_INFO, version=v), "instructions": INSTRUCTIONS})
    if method == "ping":
        return 200, _ok(rid, {})
    if method == "tools/list":
        return 200, _ok(rid, {"tools": tools_list()})
    if method == "tools/call":
        res = call_tool(cfg, app, params.get("name"), params.get("arguments") or {})
        if res is None:
            return 200, _err(rid, -32602, "Unknown tool: %s" % params.get("name"))
        return 200, _ok(rid, res)
    return 200, _err(rid, -32601, "Method not found: %s" % method)


if __name__ == "__main__":
    if sys.argv[1:] == ["--novy-kluc"]:
        from common import adb, load_cfg
        c = adb(load_cfg())
        try:
            print(new_key(c))
        finally:
            c.close()
    else:
        print("použitie: python3 mcp_api.py --novy-kluc   (vytvorí nový kľúč MCP, starý prestane platiť)")
