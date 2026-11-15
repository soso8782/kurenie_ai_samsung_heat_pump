#!/usr/bin/env python3
"""Okamžité kontakty okien priamo z MQTT (Zigbee2MQTT), iba štandardná knižnica.

Minimálny MQTT 3.1.1 klient (QoS 0): odoberá `{MQTT_BASE}/+`, z JSON správ s poľom `contact` (true = zatvorené) okamžite zapíše stav
senzora do `contacts`. Senzor sa páruje podľa mena: z2m friendly name -> slug -> `binary_sensor.<slug>_contact` (rovnako ako HA).
Pravidelné čítanie z HA (contacts.poll) ostáva ako záloha a objaví nové senzory; novšia správa z MQTT má prednosť pred starším stavom z HA
(contacts.OVERRIDE). Konfigurácia v config.env: MQTT_HOST, MQTT_PORT (1883), MQTT_USER, MQTT_PASS, MQTT_BASE (zigbee2mqtt).
Heslo sa nikdy nevypisuje.
"""
import json, re, socket, struct, sys, threading, time, unicodedata

import contacts
from common import adb, get_state, load_cfg, set_state

KEEPALIVE = 60
READ_TIMEOUT = 20          # s bez dát -> pošli PINGREQ
DEAD_AFTER = 100           # s bez čohokoľvek od brokera -> nové pripojenie
MAP_TTL = 60
RELOAD_CHECK = 5          # s: ako často sa kontroluje signál na opätovné načítanie konfigurácie (state `mqtt_reload`)
IDLE_RELOAD = 10           # s: v kľudovom režime (bez MQTT_HOST) sa config načíta znova
STATE_EVERY = 10           # s: najčastejší zápis `mqtt_last_msg` / `mqtt_last_contact`
status = {"connected": False, "last_msg": 0.0, "last_contact": 0.0, "error": ""}
_wr = {}                   # kľúč -> (posledná zapísaná hodnota, čas): stav spojenia pre web (iný proces) cez tabuľku `state`
matched = set()            # entity_id okien, ktoré mali správu z MQTT


def put_state(c, k, v, every=0, now=None):
    """Zapíše state `k` len pri zmene hodnoty (a najviac raz za `every` s). Chyba zápisu agenta nikdy nezhodí."""
    now = now if now is not None else time.time()
    v = str(v)
    old = _wr.get(k)
    if old and (old[0] == v or now - old[1] < every):
        return False
    try:
        set_state(c, k, v)
    except Exception:
        return False
    _wr[k] = (v, now)
    return True


def slug(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def _enc_len(n):
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _str(s):
    b = s.encode("utf-8")
    return struct.pack(">H", len(b)) + b


def build_connect(client_id, user, password):
    flags = 0x02 | (0x80 if user else 0) | (0x40 if user and password else 0)
    body = _str("MQTT") + bytes([4, flags]) + struct.pack(">H", KEEPALIVE) + _str(client_id)
    if user:
        body += _str(user)
        if password:
            body += _str(password)
    return b"\x10" + _enc_len(len(body)) + body


def build_subscribe(pid, topic):
    body = struct.pack(">H", pid) + _str(topic) + b"\x00"
    return b"\x82" + _enc_len(len(body)) + body


def _read_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("spojenie zatvorené")
        buf += chunk
    return buf


def read_packet(sock):
    """(typ, flags, telo). socket.timeout prepúšťa volajúcemu."""
    first = sock.recv(1)
    if not first:
        raise ConnectionError("spojenie zatvorené")
    mult, n = 1, 0
    for _ in range(4):
        b = _read_exact(sock, 1)[0]
        n += (b & 0x7F) * mult
        if not b & 0x80:
            break
        mult *= 128
    else:
        raise ValueError("zlá dĺžka paketu")
    if n > 1_000_000:
        raise ValueError("príliš veľký paket")
    return first[0] >> 4, first[0] & 0x0F, _read_exact(sock, n) if n else b""


def parse_publish(flags, body):
    """-> (topic, payload bytes, retain). Iba QoS 0 (iné sa ignoruje -> None)."""
    qos = (flags >> 1) & 3
    tl = struct.unpack(">H", body[:2])[0]
    topic = body[2:2 + tl].decode("utf-8", "replace")
    off = 2 + tl + (2 if qos else 0)
    return topic, body[off:], bool(flags & 1)


def contact_state(payload):
    """JSON správa z2m -> 'on' (otvorené) / 'off' (zatvorené) / None."""
    try:
        d = json.loads(payload.decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(d, dict) or not isinstance(d.get("contact"), bool):
        return None
    return "off" if d["contact"] else "on"


class Mapper:
    """slug z2m mena -> entity_id v `contacts` (obnovuje sa raz za minútu)."""
    def __init__(self):
        self.ts, self.m = 0.0, {}

    def get(self, c, name, now):
        if now - self.ts > MAP_TTL:
            self.m = {}
            for (eid,) in c.execute("SELECT entity_id FROM contacts WHERE entity_id LIKE 'binary_sensor.%\\_contact' ESCAPE '\\'"):
                self.m[eid[len("binary_sensor."):-len("_contact")]] = eid
            self.ts = now
        return self.m.get(slug(name))


def handle(c, mapper, base, topic, payload, retain, now=None):
    """Spracuje jednu správu. Vracia entity_id, ak sa stav zmenil, inak None."""
    now = int(now or time.time())
    if retain or not topic.startswith(base + "/"):
        return None
    name = topic[len(base) + 1:]
    if "/" in name:
        return None
    st = contact_state(payload)
    if st is None:
        return None
    eid = mapper.get(c, name, now)
    if not eid:
        return None
    status["last_contact"] = now
    contacts.OVERRIDE[eid] = (st, now)
    put_state(c, "mqtt_last_contact", now, STATE_EVERY, now)
    if eid not in matched:
        matched.add(eid)
        put_state(c, "mqtt_matched", json.dumps(sorted(matched)))
    return eid if contacts.set_one(c, eid, st, now) else None


def run_once(cfg, stop=None, reload0=None):
    host, user, pw = cfg.get("MQTT_HOST", "").strip(), cfg.get("MQTT_USER", "").strip(), cfg.get("MQTT_PASS", "")
    try:
        port = int(cfg.get("MQTT_PORT") or 1883)
    except ValueError:
        port = 1883
    base = (cfg.get("MQTT_BASE") or "zigbee2mqtt").strip().strip("/")
    sock = socket.create_connection((host, port), timeout=10)
    c = adb(cfg)
    try:
        if reload0 is None:
            reload0 = get_state(c, "mqtt_reload")
        sock.settimeout(10)
        sock.sendall(build_connect("kurenie-okna-%d" % (int(time.time()) % 100000), user, pw))
        t, _, body = read_packet(sock)
        if t != 2 or len(body) < 2 or body[1] != 0:
            raise ConnectionError("broker odmietol pripojenie (kód %s)" % (body[1] if len(body) > 1 else "?"))
        sock.sendall(build_subscribe(1, base + "/+"))
        status.update(connected=True, error="")
        put_state(c, "mqtt_error", "")
        put_state(c, "mqtt_connected", "1")
        sock.settimeout(READ_TIMEOUT)
        mapper, last_rx, last_ping, last_rl = Mapper(), time.time(), time.time(), time.time()
        while not (stop and stop.is_set()):
            try:
                t, flags, body = read_packet(sock)
                last_rx = time.time()
                status["last_msg"] = last_rx
                if t == 3:
                    put_state(c, "mqtt_last_msg", int(last_rx), STATE_EVERY, last_rx)
                    topic, payload, retain = parse_publish(flags, body)
                    handle(c, mapper, base, topic, payload, retain)
            except socket.timeout:
                pass
            now = time.time()
            if now - last_rl >= RELOAD_CHECK:
                last_rl = now
                if get_state(c, "mqtt_reload") != reload0:
                    return        # web uložil novú konfiguráciu: odpoj sa, mqtt_loop sa pripojí znova
            if now - last_rx > DEAD_AFTER:
                raise ConnectionError("broker mlčí")
            if now - last_ping >= KEEPALIVE / 2:
                sock.sendall(b"\xc0\x00")
                last_ping = now
    finally:
        status["connected"] = False
        put_state(c, "mqtt_connected", "0")
        try:
            c.close()
        finally:
            sock.close()


def _put_cfg(cfg, **kv):
    """Zápis stavu mimo spojenia (chyba, kľudový režim)."""
    if not cfg:
        return
    try:
        c = adb(cfg)
        try:
            for k, v in kv.items():
                put_state(c, k, v)
        finally:
            c.close()
    except Exception:
        pass


def mqtt_loop():
    """Daemon vlákno. Bez MQTT_HOST ostáva v kľudovom režime a každých 10 s znova načíta config (nové uloženie ho spustí).
    Pri chybe sa po pauze pripojí znova (5 -> 60 s); signál `mqtt_reload` v state spojenie ukončí a pripojí sa s novou konfiguráciou."""
    time.sleep(12)
    delay = 5
    while True:
        cfg, t0 = None, time.time()
        try:
            cfg = load_cfg()
            c = adb(cfg)
            try:
                reload0 = get_state(c, "mqtt_reload")   # pred načítaním configu, aby sa signál nestratil
            finally:
                c.close()
            cfg = load_cfg()
            if not cfg.get("MQTT_HOST", "").strip():
                status.update(connected=False, error="")
                _put_cfg(cfg, mqtt_connected="0", mqtt_error="")
                time.sleep(IDLE_RELOAD)
                continue
            run_once(cfg, reload0=reload0)
            delay = 5
            time.sleep(1)
            continue
        except Exception as e:
            status["error"] = "%s: %s" % (type(e).__name__, str(e)[:120])   # heslo sa do textu nikdy nedostane
            status["connected"] = False
            _put_cfg(cfg, mqtt_connected="0", mqtt_error=status["error"])
            print(time.strftime("%F %T"), "mqtt kontakty:", status["error"], file=sys.stderr, flush=True)
            delay = 5 if time.time() - t0 > 120 else min(60, delay * 2)
        time.sleep(delay)


def start():
    threading.Thread(target=mqtt_loop, name="contacts-mqtt", daemon=True).start()
