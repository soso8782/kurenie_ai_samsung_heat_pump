#!/usr/bin/env python3
"""Okná a dvere (SPEC_OKNA.md): kontaktné senzory z Home Assistanta, skupiny, aliasy, sezóna leto/zima a upozornenia.

Iba ČÍTA z Home Assistanta (POST /api/template s dlhodobým tokenom). Do ESP ani do HA nič nezapisuje.
Slučka `contacts_loop` beží ako daemon vlákno v procese agenta. Iba štandardná knižnica.
"""
import json, time, traceback, unicodedata, urllib.error, urllib.request
from datetime import datetime, timezone
import profil
from common import ROOMS, ROOM_NAMES, S, adb, get_state, load_cfg, set_state, tcq
import notify

DEVICE_CLASSES = ("door", "window", "opening", "garage_door")
POLL_S = 20
FAILS_ALERT = 3                  # poplach „ha“ po toľkých neúspechoch po sebe
RETRY_FAILED_S = 300             # neodoslané (zlyhané) upozornenie na otvorenie skúsiť znova po 5 min
SEASONS = ("zima", "leto")

# Šablóna Jinja pre POST {HA_URL}/api/template. Vráti JSON zoznam všetkých binary_sensor s device_class door/window/opening/garage_door.
TEMPLATE = (
    "{%- set ns = namespace(r=[]) -%}"
    "{%- for s in states.binary_sensor if s.attributes.device_class in ['door','window','opening','garage_door'] -%}"
    "{%- set ns.r = ns.r + [{'id': s.entity_id, 'name': s.name, 'dc': s.attributes.device_class, 'state': s.state, "
    "'since': s.last_changed.isoformat(), 'area': area_name(s.entity_id)}] -%}"
    "{%- endfor -%}"
    "{{ ns.r | tojson }}"
)

OTHER_NAME = "Ostatné"
_P = profil.load()
# predvolené skupiny: izby z profilu (v poradí profilu), potom Chodba a Ostatné; predvolený profil = pôvodné skupiny z v1
_LEGACY_ORDER = ("obyvacka", "detska", "jedalen", "kuchyna", "spalna", "pracovna", "kupelna")
_rooms = list(profil.room_keys(_P))
if set(_rooms) == set(_LEGACY_ORDER):
    _rooms = list(_LEGACY_ORDER)
DEFAULT_GROUPS = [(ROOM_NAMES[k], k) for k in _rooms] + [("Chodba", None), ("Ostatné", None)]
# kľúčové slová (bez diakritiky, malé písmená) -> kľúč izby; "chodba" = skupina Chodba (bez izby)
_LEGACY_KW = {"obyvacka": "obyvack", "detska": "detsk", "jedalen": "jedal", "kuchyna": "kuchyn", "spalna": "spaln",
              "pracovna": "pracovn", "kupelna": "kupel"}


def _room_keyword(key, name):
    if key in _LEGACY_KW:
        return _LEGACY_KW[key]
    n = "".join(ch for ch in unicodedata.normalize("NFKD", str(name or key).lower()) if not unicodedata.combining(ch)).strip()
    n = (n.split() or [key])[0]            # prvé slovo názvu; bez posledného písmena sa zhodne aj s ohýbanými tvarmi
    return n[:max(4, len(n) - 1)]


KEYWORDS = tuple((_room_keyword(k, ROOM_NAMES[k]), k) for k in _rooms) + (("chodb", "chodba"),)
FORCE_OTHER = ("skoda", "octavia", "umyvacka", "rura")      # auto a spotrebiče: skupina „Ostatné“, predvolene vypnuté
DEFAULT_OFF = FORCE_OTHER + ("radiator",)                    # hlavica radiátora len odhaduje okno: predvolene vypnutá
# voliteľné: konkrétny pokazený senzor pôvodného domu; ak taká entita v HA neexistuje, nestane sa nič
KITCHEN_DOOR = "binary_sensor.kuchyna_dvere_kontakt_sensor_contact"
KITCHEN_DOOR_NOTE = "pokazený – odpadnutý magnet (10. 11. 2026)"
_UNSET = object()


def norm(s):
    """Malé písmená bez diakritiky."""
    s = unicodedata.normalize("NFKD", str(s or "").lower())
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def sk(v, nd=1):
    return "–" if v is None else ("%.*f" % (nd, v)).replace(".", ",")


# ---------------------------------------------------------------- stav a skupiny

def season(c):
    v = get_state(c, "season", "zima")
    return v if v in SEASONS else "zima"


def _int_state(c, key, default, lo, hi):
    try:
        v = int(float(get_state(c, key, default)))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def notify_min(c):
    return _int_state(c, "window_notify_min", 10, 1, 240)


def repeat_min(c):
    return _int_state(c, "window_repeat_min", 30, 0, 240)


def ensure(c):
    """Pri prvom použití naplní predvolené skupiny (len ak je tabuľka prázdna) a zapamätá si id skupiny „Ostatné“."""
    if c.execute("SELECT count(*) FROM contact_groups").fetchone()[0] == 0:
        try:
            if not c.in_transaction:
                c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT count(*) FROM contact_groups").fetchone()[0] == 0:
                for i, (name, room) in enumerate(DEFAULT_GROUPS):
                    c.execute("INSERT INTO contact_groups(name, room, notify_min, sort) VALUES(?,?,NULL,?)", (name, room, i))
            c.commit()
        except Exception:
            try:
                c.rollback()
            except Exception:
                pass
            raise
    oid = other_id(c)
    return oid


def other_id(c):
    """Id skupiny „Ostatné“ (uložené v state; ak chýba, nájde sa podľa názvu alebo sa vytvorí)."""
    try:
        oid = int(get_state(c, "other_group_id", "") or 0)
    except ValueError:
        oid = 0
    if oid and c.execute("SELECT 1 FROM contact_groups WHERE id=?", (oid,)).fetchone():
        return oid
    r = c.execute("SELECT id FROM contact_groups WHERE name=? ORDER BY id LIMIT 1", (OTHER_NAME,)).fetchone()
    if r:
        oid = r[0]
    else:
        cur = c.execute("INSERT INTO contact_groups(name, room, notify_min, sort) VALUES(?,NULL,NULL,"
                        "(SELECT coalesce(max(sort),0)+1 FROM contact_groups))", (OTHER_NAME,))
        c.commit()
        oid = cur.lastrowid
    set_state(c, "other_group_id", oid)
    return oid


GCOLS = ("id", "name", "room", "notify_min", "sort")


def groups(c):
    ensure(c)
    return [dict(zip(GCOLS, r)) for r in c.execute("SELECT id, name, room, notify_min, sort FROM contact_groups ORDER BY sort, id")]


def group_by_id(c, gid):
    r = c.execute("SELECT id, name, room, notify_min, sort FROM contact_groups WHERE id=?", (gid,)).fetchone()
    return dict(zip(GCOLS, r)) if r else None


def _valid_notify_min(v, what="Čas upozornenia"):
    if v in (None, ""):
        return None
    if isinstance(v, bool) or (isinstance(v, float) and v != int(v)):
        raise ValueError("%s musí byť celé číslo minút od 1 do 240." % what)
    try:
        n = int(str(v).strip()) if not isinstance(v, (int, float)) else int(v)
    except ValueError:
        raise ValueError("%s musí byť celé číslo minút od 1 do 240." % what)
    if not 1 <= n <= 240:
        raise ValueError("%s musí byť od 1 do 240 minút." % what)
    return n


def group_save(c, name, room=None, notify_min_=None, gid=None):
    """Nová skupina (gid None) alebo úprava. Vracia id. Pri neplatnom vstupe vyhodí ValueError so slovenským textom."""
    ensure(c)
    name = str(name or "").strip()
    if not name or len(name) > 40:
        raise ValueError("Názov skupiny musí mať 1 až 40 znakov.")
    room = room or None
    if room is not None and room not in ROOMS:
        raise ValueError("Neplatná izba.")
    nm = _valid_notify_min(notify_min_)
    dup = c.execute("SELECT id FROM contact_groups WHERE lower(name)=lower(?)", (name,)).fetchall()
    if gid is None:
        if dup:
            raise ValueError("Skupina s týmto názvom už existuje.")
        cur = c.execute("INSERT INTO contact_groups(name, room, notify_min, sort) VALUES(?,?,?,(SELECT coalesce(max(sort),0)+1 FROM contact_groups))",
                        (name, room, nm))
        c.commit()
        return cur.lastrowid
    old = group_by_id(c, gid)
    if not old:
        raise ValueError("Skupina neexistuje.")
    if any(r[0] != gid for r in dup):
        raise ValueError("Skupina s týmto názvom už existuje.")
    if gid == other_id(c) and name != old["name"]:
        raise ValueError("Skupinu „%s“ nemožno premenovať." % old["name"])
    c.execute("UPDATE contact_groups SET name=?, room=?, notify_min=? WHERE id=?", (name, room, nm, gid))
    c.commit()
    return gid


def group_delete(c, gid):
    """Zmaže skupinu, senzory presunie do „Ostatné“. „Ostatné“ sa zmazať nedá (ValueError)."""
    oid = ensure(c)
    if not group_by_id(c, gid):
        raise ValueError("Skupina neexistuje.")
    if gid == oid:
        raise ValueError("Skupinu „%s“ nemožno zmazať." % OTHER_NAME)
    c.execute("UPDATE contacts SET group_id=? WHERE group_id=?", (oid, gid))
    c.execute("DELETE FROM contact_groups WHERE id=?", (gid,))
    c.commit()


def guess_group(c, entity_id, name, area):
    """Id skupiny pre nový senzor podľa oblasti (area) alebo mena; auto a spotrebiče idú do „Ostatné“."""
    oid = other_id(c)
    blob = norm(" ".join((entity_id or "", name or "")))
    if any(k in blob for k in FORCE_OTHER):
        return oid
    for text in (area, name, entity_id):
        t = norm(text)
        for kw, key in KEYWORDS:
            if kw in t:
                if key == "chodba":
                    r = c.execute("SELECT id FROM contact_groups WHERE lower(name)='chodba' ORDER BY id LIMIT 1").fetchone()
                else:
                    r = c.execute("SELECT id FROM contact_groups WHERE room=? ORDER BY sort, id LIMIT 1", (key,)).fetchone()
                return r[0] if r else oid
    return oid


def default_enabled(c, entity_id, name, gid):
    if gid == other_id(c):
        return 0
    blob = norm(entity_id) + " " + norm(name)
    return 0 if any(k in blob for k in DEFAULT_OFF) else 1


# ---------------------------------------------------------------- Home Assistant (čítanie)

def ha_conf(cfg):
    return (cfg.get("HA_URL") or "").strip().rstrip("/"), (cfg.get("HA_TOKEN") or "").strip()


def parse_since(s):
    """ISO čas z HA (napr. 2026-11-10T18:03:11.123456+00:00) -> unix sekundy; None pri neplatnom."""
    try:
        t = str(s).strip().replace("Z", "+00:00")
        d = datetime.fromisoformat(t)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return int(d.timestamp())
    except (ValueError, TypeError):
        return None


def norm_state(s):
    return "on" if s == "on" else ("off" if s == "off" else "unavailable")


def fetch(cfg, timeout=10):
    """Zoznam senzorov z HA: [{"id","name","dc","state","since","area"}]. Pri chybe vyhodí RuntimeError so slovenským textom
    (token sa v texte nikdy nevyskytuje)."""
    url, token = ha_conf(cfg)
    if not url or not token:
        raise RuntimeError("Home Assistant nie je nastavený (chýba adresa alebo token).")
    req = urllib.request.Request(url + "/api/template", data=json.dumps({"template": TEMPLATE}).encode("utf-8"), method="POST",
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(4_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise RuntimeError("Home Assistant odmietol token (HTTP %d). Skontroluj dlhodobý prístupový token." % e.code)
        if e.code == 404:
            raise RuntimeError("Na tejto adrese nie je API Home Assistanta (HTTP 404). Skontroluj adresu.")
        raise RuntimeError("Home Assistant odpovedal chybou HTTP %d." % e.code)
    except urllib.error.URLError as e:
        raise RuntimeError("Spojenie s Home Assistantom zlyhalo: %s" % str(getattr(e, "reason", e))[:150])
    except Exception as e:
        raise RuntimeError("Spojenie s Home Assistantom zlyhalo: %s" % (type(e).__name__ + " " + str(e))[:150])
    try:
        data = json.loads(raw.strip())
    except ValueError:
        raise RuntimeError("Home Assistant nevrátil platný JSON (šablóna sa nevyhodnotila).")
    if not isinstance(data, list):
        raise RuntimeError("Home Assistant vrátil neočakávaný tvar odpovede.")
    out = []
    for d in data:
        if isinstance(d, dict) and isinstance(d.get("id"), str) and d["id"].startswith("binary_sensor."):
            out.append(d)
    return out


def _set_ha_state(c, ok, error=None, now=None, count=None):
    now = int(now or time.time())
    if ok:
        if count is not None:
            set_state(c, "ha_count", count)
        set_state(c, "ha_fails", 0)
        set_state(c, "ha_last_ok", now)
        set_state(c, "ha_error", "")
    else:
        try:
            n = int(get_state(c, "ha_fails", "0") or 0)
        except ValueError:
            n = 0
        set_state(c, "ha_fails", n + 1)
        set_state(c, "ha_error", str(error or "")[:300])


def apply_items(c, items, now=None):
    """Zapíše stav senzorov z HA do `contacts` a zmeny stavu do `contact_events` (čas = last_changed z HA).
    Senzor, ktorý v zozname chýba, ostane s kľúčom „nedostupné“. Vracia počet nových senzorov."""
    now = int(now or time.time())
    ensure(c)
    oid = other_id(c)
    seen, new = set(), 0
    for d in items:
        eid = d["id"]
        seen.add(eid)
        name = str(d.get("name") or eid)[:200]
        dc = str(d.get("dc") or "")[:40]
        st = norm_state(d.get("state"))
        since = parse_since(d.get("since")) or now
        ov = OVERRIDE.get(eid)
        if ov and ov[1] > since:   # novšia správa priamo z MQTT než stav, ktorý HA ešte nedoručil
            st, since = ov[0], ov[1]
        area = d.get("area")
        area = str(area)[:100] if area else None
        row = c.execute("SELECT state, since, group_id FROM contacts WHERE entity_id=?", (eid,)).fetchone()
        if row is None:
            gid = guess_group(c, eid, name, area)
            enabled = default_enabled(c, eid, name, gid)
            note = None
            if eid == KITCHEN_DOOR:
                enabled, note = 0, KITCHEN_DOOR_NOTE
            c.execute("INSERT INTO contacts(entity_id, ha_name, alias, group_id, enabled, device_class, state, since, area, note, first_seen, last_seen) "
                      "VALUES(?,?,NULL,?,?,?,?,?,?,?,?,?)", (eid, name, gid, enabled, dc, st, since, area, note, now, now))
            new += 1
        else:
            gid = row[2]
            if gid is None or not group_by_id(c, gid):
                gid = oid
            c.execute("UPDATE contacts SET ha_name=?, device_class=?, state=?, since=?, area=?, group_id=?, last_seen=? WHERE entity_id=?",
                      (name, dc, st, since, area, gid, now, eid))
        _record_event(c, eid, st, since)
    for (eid,) in c.execute("SELECT entity_id FROM contacts").fetchall():
        if eid in seen:
            continue
        r = c.execute("SELECT state FROM contacts WHERE entity_id=?", (eid,)).fetchone()
        if r and r[0] != "unavailable":
            c.execute("UPDATE contacts SET state='unavailable', since=? WHERE entity_id=?", (now, eid))
            _record_event(c, eid, "unavailable", now)
    c.commit()
    return new


OVERRIDE = {}   # entity_id -> (stav, čas): posledná správa z MQTT (contacts_mqtt), má prednosť pred starším stavom z HA


def set_one(c, eid, st, ts):
    """Okamžitý zápis jedného senzora (z MQTT). True, ak sa stav zmenil; neznámy senzor sa ignoruje (objaví ho poll z HA)."""
    ts = int(ts)
    row = c.execute("SELECT state FROM contacts WHERE entity_id=?", (eid,)).fetchone()
    if row is None:
        return False
    if row[0] == st:
        c.execute("UPDATE contacts SET last_seen=? WHERE entity_id=?", (ts, eid))
        c.commit()
        return False
    c.execute("UPDATE contacts SET state=?, since=?, last_seen=? WHERE entity_id=?", (st, ts, ts, eid))
    _record_event(c, eid, st, ts)
    c.commit()
    return True


def _record_event(c, eid, state, ts):
    last = c.execute("SELECT state, ts FROM contact_events WHERE entity_id=? ORDER BY ts DESC, id DESC LIMIT 1", (eid,)).fetchone()
    if last is None or last[0] != state:
        c.execute("INSERT INTO contact_events(ts, entity_id, state) VALUES(?,?,?)", (max(int(ts), last[1] if last else 0), eid, state))


def poll(cfg, c, now=None):
    """Jedno čítanie z HA. Vracia {"ok", "count", "error"}; stav spojenia (ha_fails, ha_last_ok, ha_error) uloží do `state`."""
    now = int(now or time.time())
    url, token = ha_conf(cfg)
    if not url or not token:
        for k in ("ha_fails",):
            if get_state(c, k, "0") != "0":
                set_state(c, k, 0)
        return {"ok": False, "count": 0, "error": "nenastavené"}
    try:
        items = fetch(cfg)
        apply_items(c, items, now)
        _set_ha_state(c, True, now=now, count=len(items))
        return {"ok": True, "count": len(items), "error": None}
    except Exception as e:
        err = str(e) if isinstance(e, RuntimeError) else "%s: %s" % (type(e).__name__, str(e)[:200])
        try:
            _set_ha_state(c, False, err, now)
        except Exception:
            traceback.print_exc()
        return {"ok": False, "count": 0, "error": err}


def test(cfg):
    """Pre web (/api/ha_test): len prečíta zoznam, nič nezapisuje. Vracia {"ok","count","error"}."""
    try:
        items = fetch(cfg)
        return {"ok": True, "count": len(items), "error": None}
    except Exception as e:
        return {"ok": False, "count": 0, "error": str(e)[:300]}


def ha_status(cfg, c):
    url, token = ha_conf(cfg)
    try:
        fails = int(get_state(c, "ha_fails", "0") or 0)
    except ValueError:
        fails = 0
    try:
        last_ok = int(float(get_state(c, "ha_last_ok", "") or 0)) or None
    except ValueError:
        last_ok = None
    configured = bool(url and token)
    try:
        count = int(get_state(c, "ha_count", "")) if configured and last_ok else None
    except ValueError:
        count = None
    ok = bool(configured and last_ok and fails == 0 and time.time() - last_ok < 120)
    err = (get_state(c, "ha_error", "") or None) if configured and fails else None
    if configured and not ok and not err:
        err = "Čakám na prvé čítanie z Home Assistanta." if not last_ok else "Údaje z Home Assistanta sú staré (agent ho dlhšie nečítal)."
    return {"configured": configured, "ok": ok, "last_ok": last_ok, "count": count, "error": err,
            "token_hint": ("…" + token[-4:]) if len(token) > 12 else "", "url": url}


def ha_alert(c):
    """Text poplachu `ha` (po 3 neúspešných čítaniach po sebe) alebo None."""
    try:
        fails = int(get_state(c, "ha_fails", "0") or 0)
    except ValueError:
        return None
    if fails < FAILS_ALERT:
        return None
    return "Home Assistant neodpovedá (%d pokusy po sebe), okná a dvere sa teraz nesledujú. %s" % (fails, get_state(c, "ha_error", "") or "")


# ---------------------------------------------------------------- zoznamy a úpravy

SCOLS = ("entity_id", "ha_name", "alias", "group_id", "enabled", "device_class", "state", "since", "area", "note")


def label(c, entity_id):
    r = c.execute("SELECT alias, ha_name FROM contacts WHERE entity_id=?", (entity_id,)).fetchone()
    if not r:
        return entity_id
    return (r[0] or "").strip() or r[1] or entity_id


def list_contacts(c, now=None):
    """Všetky senzory ako slovníky (s `label`, `open_min`)."""
    now = int(now or time.time())
    ensure(c)
    out = []
    for r in c.execute("SELECT %s FROM contacts ORDER BY lower(coalesce(nullif(alias,''), ha_name)), entity_id" % ", ".join(SCOLS)):
        d = dict(zip(SCOLS, r))
        d["enabled"] = bool(d["enabled"])
        d["label"] = (d["alias"] or "").strip() or d["ha_name"] or d["entity_id"]
        d["open_min"] = max(0, (now - d["since"]) // 60) if d["state"] == "on" and d["since"] else None
        out.append(d)
    return out


def tree(c, now=None):
    """Skupiny so senzormi + senzory bez platnej skupiny (pre GET /api/contacts)."""
    gs = groups(c)
    byg = {g["id"]: dict(g, sensors=[]) for g in gs}
    ungrouped = []
    for d in list_contacts(c, now):
        (byg[d["group_id"]]["sensors"] if d["group_id"] in byg else ungrouped).append(d)
    return [byg[g["id"]] for g in gs], ungrouped


def update_contact(c, entity_id, alias=_UNSET, group_id=_UNSET, enabled=_UNSET, note=_UNSET):
    """Úprava senzora. Neplatný vstup = ValueError so slovenským textom."""
    ensure(c)
    if not c.execute("SELECT 1 FROM contacts WHERE entity_id=?", (entity_id,)).fetchone():
        raise ValueError("Neznámy senzor.")
    sets, args = [], []
    if alias is not _UNSET:
        if alias is not None and not isinstance(alias, str):
            raise ValueError("Meno musí byť text.")
        a = (alias or "").strip()
        if len(a) > 60:
            raise ValueError("Meno môže mať najviac 60 znakov.")
        sets.append("alias=?"); args.append(a or None)
    if group_id is not _UNSET:
        if group_id is None:
            group_id = other_id(c)
        if isinstance(group_id, bool) or not isinstance(group_id, int) or not group_by_id(c, group_id):
            raise ValueError("Neplatná skupina.")
        sets.append("group_id=?"); args.append(group_id)
    if enabled is not _UNSET:
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() in ("1", "true", "yes", "on")
        elif not isinstance(enabled, (bool, int)):
            raise ValueError("Neplatná hodnota zapnutia.")
        sets.append("enabled=?"); args.append(1 if enabled else 0)
    if note is not _UNSET:
        if note is not None and not isinstance(note, str):
            raise ValueError("Poznámka musí byť text.")
        n = (note or "").strip()
        if len(n) > 200:
            raise ValueError("Poznámka môže mať najviac 200 znakov.")
        sets.append("note=?"); args.append(n or None)
    if not sets:
        raise ValueError("Nič na uloženie.")
    c.execute("UPDATE contacts SET %s WHERE entity_id=?" % ", ".join(sets), (*args, entity_id))
    c.commit()


def save_settings(c, body):
    """Sezóna, čas upozornenia, pripomienky. Vracia text zmeny pre záznam; neplatný vstup = ValueError."""
    upd, what = {}, []
    if body.get("season") is not None:
        if body["season"] not in SEASONS:
            raise ValueError("Sezóna musí byť „zima“ alebo „leto“.")
        upd["season"] = body["season"]
        what.append("sezóna: " + body["season"])
    for key, sk_, lo, text in (("notify_min", "window_notify_min", 1, "Čas upozornenia"), ("repeat_min", "window_repeat_min", 0, "Pripomienka")):
        if body.get(key) is None:
            continue
        v = body[key]
        if isinstance(v, bool) or (isinstance(v, float) and v != int(v)):
            raise ValueError("%s musí byť celé číslo minút." % text)
        try:
            n = int(str(v).strip()) if isinstance(v, str) else int(v)
        except (TypeError, ValueError):
            raise ValueError("%s musí byť celé číslo minút." % text)
        if not lo <= n <= 240:
            raise ValueError("%s musí byť od %d do 240 minút." % (text, lo))
        upd[sk_] = n
        what.append("%s %d min" % (text.lower(), n))
    if not upd:
        raise ValueError("Nič na uloženie.")
    ensure(c)
    for k, v in upd.items():
        set_state(c, k, v)
    return ", ".join(what)


# ---------------------------------------------------------------- intervaly otvorenia

def _merge(ivs):
    out = []
    for s, e in sorted(ivs):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def _clip_sum(ivs, a, b):
    t = 0
    for s, e in ivs:
        lo, hi = max(s, a), min(e, b)
        if hi > lo:
            t += hi - lo
    return t


def _mins(sec):
    return round(sec / 60.0, 1)


def _intervals(c, a, b, now=None, only_enabled=True):
    """Intervaly otvorenia v [a, b) pre každý senzor: [{"entity_id","group_id","room","group","intervals":[[s,e],...]}].
    Začiatok intervalu pred `a` sa dopočíta z posledného stavu pred `a`; otvorené okno sa počíta najviac po `now`."""
    now = int(now or time.time())
    a, b = int(a), int(b)
    cap = min(b, now)
    ensure(c)
    meta = {}
    for eid, gid, en in c.execute("SELECT entity_id, group_id, enabled FROM contacts"):
        if only_enabled and not en:
            continue
        meta[eid] = gid
    gmap = {g["id"]: g for g in groups(c)}
    oid = other_id(c)
    ev = {}
    for eid, st, ts in c.execute("SELECT entity_id, state, max(ts) FROM contact_events WHERE ts<? GROUP BY entity_id", (a,)):
        ev.setdefault(eid, []).append((a, st))   # stav platný od `a`
    for eid, ts, st in c.execute("SELECT entity_id, ts, state FROM contact_events WHERE ts>=? AND ts<? ORDER BY ts, id", (a, b)):
        ev.setdefault(eid, []).append((ts, st))
    out = []
    for eid, trans in ev.items():
        if eid not in meta:
            continue
        ivs = []
        for i, (ts, st) in enumerate(trans):
            if st != "on":
                continue
            end = trans[i + 1][0] if i + 1 < len(trans) else cap
            s, e = max(ts, a), min(end, cap)
            if e > s:
                ivs.append([s, e])
        gid = meta[eid] if meta[eid] in gmap else oid
        g = gmap.get(gid) or {}
        out.append({"entity_id": eid, "group_id": gid, "room": g.get("room"), "group": g.get("name"), "intervals": _merge(ivs)})
    return out


def open_minutes(c, a, b, only_enabled=True, now=None):
    """(`{group_id: minúty}`, `{room: minúty}`) otvorenia v [a, b). Prekrývajúce sa okná v skupine (aj v izbe) sa počítajú
    ako zjednotenie času. Skupiny a izby bez otvorenia v slovníku nie sú."""
    byg, byr = {}, {}
    for it in _intervals(c, a, b, now, only_enabled):
        byg.setdefault(it["group_id"], []).extend(it["intervals"])
        if it["room"]:
            byr.setdefault(it["room"], []).extend(it["intervals"])
    res_g = {k: _mins(_clip_sum(_merge(v), a, b)) for k, v in byg.items()}
    res_r = {k: _mins(_clip_sum(_merge(v), a, b)) for k, v in byr.items()}
    return {k: v for k, v in res_g.items() if v > 0}, {k: v for k, v in res_r.items() if v > 0}


def open_minutes_total(c, a, b, only_enabled=True, now=None):
    """Minúty, keď bol otvorený aspoň jeden senzor (zjednotenie cez celý dom)."""
    allv = []
    for it in _intervals(c, a, b, now, only_enabled):
        allv.extend(it["intervals"])
    return _mins(_clip_sum(_merge(allv), a, b))


def open_minutes_sensor(c, a, b, only_enabled=True, now=None):
    """`{entity_id: minúty}` otvorenia každého senzora v [a, b) (len tie s otvorením)."""
    r = {it["entity_id"]: _mins(_clip_sum(it["intervals"], a, b)) for it in _intervals(c, a, b, now, only_enabled)}
    return {k: v for k, v in r.items() if v > 0}


def hourly_open(c, a, b, now=None):
    """Pre každú hodinu od `a`: {"ts", "by": {izba_alebo_názov_skupiny: minúty}, "total": minúty (zjednotenie cez dom)}."""
    items = _intervals(c, a, b, now)
    keyed, allv = {}, []
    for it in items:
        key = it["room"] or it["group"] or "?"
        keyed.setdefault(key, []).extend(it["intervals"])
        allv.extend(it["intervals"])
    keyed = {k: _merge(v) for k, v in keyed.items()}
    allv = _merge(allv)
    out = []
    n = max(1, int((b - a + 3599) // 3600))
    for i in range(n):
        ha, hb = a + i * 3600, min(a + (i + 1) * 3600, b)
        by = {k: _mins(_clip_sum(v, ha, hb)) for k, v in keyed.items()}
        out.append({"ts": ha, "by": {k: v for k, v in by.items() if v > 0}, "total": _mins(_clip_sum(allv, ha, hb))})
    return out


def has_data(c, before_ts):
    """True, ak existuje záznam o senzoroch z doby pred `before_ts` (inak je „0 minút“ len nedostatok dát)."""
    return c.execute("SELECT 1 FROM contact_events WHERE ts<? LIMIT 1", (int(before_ts),)).fetchone() is not None


def window_minutes(c, a, b, now=None):
    """Minúty otvorenia celého domu v [a, b) alebo None, ak v tom čase ešte neboli žiadne dáta zo senzorov."""
    if not has_data(c, b):
        return None
    return open_minutes_total(c, a, b, now=now)


def open_now(c, now=None):
    """Zapnuté senzory, ktoré sú práve otvorené: [{entity_id, label, group, group_id, room, minutes, since}]."""
    now = int(now or time.time())
    ensure(c)
    oid = other_id(c)
    gmap = {g["id"]: g for g in groups(c)}
    out = []
    for eid, alias, hn, gid, since in c.execute("SELECT entity_id, alias, ha_name, group_id, since FROM contacts WHERE enabled=1 AND state='on' ORDER BY since"):
        g = gmap.get(gid) or gmap.get(oid) or {}
        out.append({"entity_id": eid, "label": (alias or "").strip() or hn or eid, "group": g.get("name"), "group_id": g.get("id"),
                    "room": g.get("room"), "minutes": max(0, (now - (since or now)) // 60), "since": since})
    return out


def open_counts(c, a, b):
    """{názov skupiny: počet otvorení} zapnutých senzorov v [a, b)."""
    gmap = {g["id"]: g["name"] for g in groups(c)}
    oid = other_id(c)
    out = {}
    for gid, n in c.execute("SELECT k.group_id, count(*) FROM contact_events e JOIN contacts k ON k.entity_id=e.entity_id "
                            "WHERE k.enabled=1 AND e.state='on' AND e.ts>=? AND e.ts<? GROUP BY k.group_id", (int(a), int(b))):
        nm = gmap.get(gid) or gmap.get(oid)
        out[nm] = out.get(nm, 0) + n
    return out


def window_log(c, a, b):
    """Otvorenia a zatvorenia zapnutých senzorov v [a, b): [{ts, label, group, state}] (state on/off)."""
    gmap = {g["id"]: g["name"] for g in groups(c)}
    oid = other_id(c)
    out = []
    for ts, eid, st, alias, hn, gid in c.execute(
            "SELECT e.ts, e.entity_id, e.state, k.alias, k.ha_name, k.group_id FROM contact_events e JOIN contacts k ON k.entity_id=e.entity_id "
            "WHERE k.enabled=1 AND e.state IN ('on','off') AND e.ts>=? AND e.ts<? ORDER BY e.ts, e.id", (int(a), int(b))):
        out.append({"ts": ts, "label": (alias or "").strip() or hn or eid, "group": gmap.get(gid) or gmap.get(oid), "state": st})
    return out


def llm_text(cfg, c, now=None):
    """Sekcia „OKNÁ A DVERE“ pre vstup modelu."""
    now = int(now or time.time())
    head = "OKNÁ A DVERE (sezóna: %s):" % season(c)
    if not ha_conf(cfg)[1]:
        return head + "\nHome Assistant nie je nastavený, o oknách nevieš nič."
    enabled = [d for d in list_contacts(c, now) if d["enabled"]]
    if not enabled:
        return head + "\n(žiadne zapnuté senzory)"
    lines = [head]
    op = open_now(c, now)
    lines.append("Teraz otvorené: " + ("; ".join("%s – %s (%d min)" % (o["group"], o["label"], o["minutes"]) for o in op) if op else "nič"))
    a = now - 24 * 3600
    byg, _byr = open_minutes(c, a, now, now=now)
    cnt = open_counts(c, a, now)
    gnames = {g["id"]: g["name"] for g in groups(c)}
    parts = ["%s %s min (%d×)" % (gnames.get(gid, "?"), sk(m, 0), cnt.get(gnames.get(gid), 0)) for gid, m in sorted(byg.items(), key=lambda kv: -kv[1])]
    lines.append("Za posledných 24 h otvorené: " + (", ".join(parts) if parts else "nič"))
    off = [d["label"] for d in enabled if d["state"] == "unavailable"]
    if off:
        lines.append("Nedostupné senzory (stav neznámy): " + ", ".join(off))
    if not ha_status(cfg, c)["ok"]:
        lines.append("Pozor: Home Assistant teraz neodpovedá, údaje o oknách nemusia byť aktuálne.")
    return "\n".join(lines)


def day_open_minutes(c, a, b, now=None):
    """Pre denné súhrny: minúty otvorenia v [a, b) alebo None, ak za ten deň nie sú dáta zo senzorov."""
    return window_minutes(c, a, b, now)


# ---------------------------------------------------------------- upozornenia (zima)

def _outside_temp(cfg):
    try:
        r = tcq(cfg, "SELECT value FROM samples WHERE entity=? ORDER BY ts DESC LIMIT 1", (S.get("vonku", ""),))
        return r[0][0] if r and r[0][0] is not None else None
    except Exception:
        return None


def _load_mem(c):
    try:
        m = json.loads(get_state(c, "window_notified", "") or "{}")
        return m if isinstance(m, dict) else {}
    except ValueError:
        return {}


def _send(cfg, title, msg):
    try:
        ok, err = notify.event(cfg, "okno", title, msg)
    except Exception as e:
        return False
    return bool(ok) or err == "nenastavené"   # bez webhooku niet čo opakovať


def check_notifications(cfg, c, now=None):
    """Zimné upozornenia na otvorené okná (SPEC_OKNA.md). Pamäť o odoslaných je v `state.window_notified`
    ({entity_id: {since, sent, ok, n}}), takže reštart neposlal duplikát. V lete sa neposiela nič. Vracia počet odoslaných."""
    now = int(now or time.time())
    mem = _load_mem(c)
    if season(c) != "zima":
        if mem:
            set_state(c, "window_notified", "{}")
        return 0
    thr_global, rep = notify_min(c), repeat_min(c)
    gthr = {g["id"]: g["notify_min"] for g in groups(c)}
    sent, changed = 0, False
    temp = _outside_temp(cfg)
    ttxt = (" Vonku je %s °C." % sk(temp)) if temp is not None else ""
    rows = {r[0]: r for r in c.execute("SELECT entity_id, alias, ha_name, group_id, state, since, enabled FROM contacts")}
    gname = {g["id"]: g["name"] for g in groups(c)}
    oid = other_id(c)
    # 1) otvorené senzory
    for eid, (_e, alias, hn, gid, st, since, en) in rows.items():
        if not en or st != "on" or not since:
            continue
        lab = (alias or "").strip() or hn or eid
        gn = gname.get(gid) or gname.get(oid)
        mins = max(0, (now - since) // 60)
        thr = gthr.get(gid) or thr_global
        m = mem.get(eid)
        same = bool(m) and m.get("since") == since
        if mins < thr:
            continue
        if not same or (not m.get("ok") and now - m.get("sent", 0) >= RETRY_FAILED_S):
            if not same and m:
                m = None
            ok = _send(cfg, "Kúrenie: otvorené okno", "Otvorené okno: %s – %s už %d min.%s" % (gn, lab, mins, ttxt))
            mem[eid] = {"since": since, "sent": now, "ok": ok, "n": (m.get("n", 0) if m else 0) + 1}
            changed, sent = True, sent + 1
        elif m.get("ok") and rep > 0 and now - m.get("sent", 0) >= rep * 60:
            ok = _send(cfg, "Kúrenie: okno stále otvorené", "Otvorené okno: %s – %s už %d min.%s" % (gn, lab, mins, ttxt))
            m["sent"], m["ok"], m["n"] = now, ok or m.get("ok"), m.get("n", 1) + 1
            changed, sent = True, sent + 1
    # 2) zatvorené (alebo vypnuté / zmazané) senzory, o ktorých už odišlo upozornenie
    for eid in list(mem):
        r = rows.get(eid)
        m = mem[eid]
        if r is not None and r[6] and r[4] == "on" and r[5] == m.get("since"):
            continue                                     # stále to isté otvorenie
        if r is not None and r[6] and r[4] == "unavailable":
            if now - m.get("since", now) < 3 * 86400:
                continue                                 # stav neznámy, počkáme na správu od senzora
        if r is not None and r[6] and r[4] == "off" and m.get("ok") and m.get("n", 0) >= 1:
            lab = (r[1] or "").strip() or r[2] or eid
            closed_at = r[5] if r[5] and r[5] >= m.get("since", 0) else now
            mins = max(1, int(round((closed_at - m["since"]) / 60.0)))
            _send(cfg, "Kúrenie: okno zatvorené", "Zatvorené: %s po %d min." % (lab, mins))
            sent += 1
        del mem[eid]
        changed = True
    if changed:
        set_state(c, "window_notified", json.dumps(mem))
    return sent


# ---------------------------------------------------------------- slučka

def contacts_loop():
    """Daemon vlákno procesu agenta: každých 20 s prečíta HA a vybaví upozornenia. Nikdy nespadne."""
    time.sleep(10)
    while True:
        try:
            cfg = load_cfg()
            if ha_conf(cfg)[1]:
                c = adb(cfg)
                try:
                    poll(cfg, c)
                    check_notifications(cfg, c)
                finally:
                    c.close()
        except Exception:
            try:
                traceback.print_exc()
            except Exception:
                pass
        time.sleep(POLL_S)
