#!/usr/bin/env python3
"""Minimálny WebSocket klient pre Home Assistant (iba štandardná knižnica).

Použitie:
    cli = HAClient()
    cli.connect("https://ha.example.sk", token, timeout=30)   # http(s)://host -> ws(s)://host/api/websocket
    res = cli.call({"type": "get_config"})                     # "id" sa doplní automaticky
    cli.close()

Token sa nikdy nevypisuje ani nelogguje; chybové správy sú po slovensky a neobsahujú token.
"""
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import time
from urllib.parse import urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MSG = 64 * 1024 * 1024        # horný limit jednej správy (aj po zlepení fragmentov)
MAX_HTTP_HEAD = 16 * 1024         # limit hlavičiek odpovede na Upgrade
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class HAError(RuntimeError):
    """Chyba spojenia alebo protokolu. Správa je po slovensky a nikdy neobsahuje token."""


def ws_target(url):
    """`http(s)://host[:port][/prefix]` alebo ws/wss -> (tls, host, port, path); path = `<prefix>/api/websocket`."""
    u = urlsplit((url or "").strip())
    scheme = u.scheme.lower()
    if scheme in ("https", "wss"):
        tls = True
    elif scheme in ("http", "ws"):
        tls = False
    else:
        raise HAError("Adresa Home Assistanta (HA_URL) musí začínať na http://, https://, ws:// alebo wss://.")
    if not u.hostname:
        raise HAError("Adresa Home Assistanta (HA_URL) nemá názov počítača.")
    try:
        port = u.port or (443 if tls else 80)
    except ValueError:
        raise HAError("Adresa Home Assistanta (HA_URL) má neplatný port.") from None
    return tls, u.hostname, port, u.path.rstrip("/") + "/api/websocket"


def _why(e):
    return (str(e) or type(e).__name__)[:200]


def _xor(data, mask):
    n = len(data)
    if not n:
        return b""
    m = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(m, "big")).to_bytes(n, "big")


def _frame(opcode, payload=b"", fin=True):
    """Klientský rámec (vždy maskovaný, ako to vyžaduje RFC 6455)."""
    n = len(payload)
    head = bytearray([(0x80 if fin else 0) | opcode])
    if n < 126:
        head.append(0x80 | n)
    elif n < 65536:
        head.append(0x80 | 126)
        head += struct.pack("!H", n)
    else:
        head.append(0x80 | 127)
        head += struct.pack("!Q", n)
    mask = os.urandom(4)
    return bytes(head) + mask + _xor(payload, mask)


class HAClient:
    def __init__(self):
        self._sock = None
        self._buf = bytearray()
        self._token = ""
        self._timeout = 30.0
        self._next_id = 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ------------------------------------------------------------ spojenie
    def connect(self, url, token, timeout=30):
        """Otvorí WebSocket, prejde handshake a overenie tokenom. Chyby = HAError (bez tokenu)."""
        if not token:
            raise HAError("Chýba HA_TOKEN v config.env.")
        tls, host, port, path = ws_target(url)
        self.close()
        self._token = token
        self._timeout = float(timeout)
        self._buf = bytearray()
        self._next_id = 1
        try:
            raw = socket.create_connection((host, port), timeout=self._timeout)
        except OSError as e:
            raise HAError("Pripojenie na Home Assistant zlyhalo (%s)." % _why(e)) from None
        self._sock = raw
        try:
            if tls:
                # predvolený kontext: overuje certifikát aj názov počítača
                self._sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
            self._handshake(host, port, tls, path)
            self._authenticate()
        except HAError:
            self.close()
            raise
        except ssl.SSLError as e:
            self.close()
            raise HAError("Zabezpečené spojenie (TLS) zlyhalo: %s" % _why(e)) from None
        except OSError as e:
            self.close()
            raise HAError("Spojenie s Home Assistantom zlyhalo (%s)." % _why(e)) from None
        return self

    def close(self):
        """Pošle close rámec a zatvorí socket. Opakované volanie je bezpečné."""
        s, self._sock = self._sock, None
        if s is None:
            return
        try:
            s.sendall(_frame(OP_CLOSE, struct.pack("!H", 1000)))
        except Exception:
            pass
        try:
            s.close()
        except Exception:
            pass

    def _handshake(self, host, port, tls, path):
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        hhost = "[%s]" % host if ":" in host else host
        if port != (443 if tls else 80):
            hhost += ":%d" % port
        req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (path, hhost, key))
        self._send_raw(req.encode("ascii"))
        head = self._read_until(b"\r\n\r\n", MAX_HTTP_HEAD).decode("iso-8859-1")
        lines = head.split("\r\n")
        parts = lines[0].split(" ")
        code = parts[1] if len(parts) > 1 else "?"
        if code != "101":
            raise HAError("Home Assistant neprijal WebSocket spojenie (HTTP %s). Skontroluj HA_URL." % code)
        hdrs = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                hdrs[k.strip().lower()] = v.strip()
        expect = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
        if hdrs.get("sec-websocket-accept") != expect:
            raise HAError("Odpoveď servera nemá správny Sec-WebSocket-Accept (handshake zlyhal).")

    def _authenticate(self):
        msg = self._recv_json()
        if msg.get("type") != "auth_required":
            raise HAError("Home Assistant sa neohlásil výzvou na overenie (prišla správa typu %s)." % _kind(msg))
        self._send_json({"type": "auth", "access_token": self._token})
        msg = self._recv_json()
        typ = msg.get("type")
        if typ == "auth_ok":
            return
        if typ == "auth_invalid":
            raise HAError("Home Assistant odmietol prístupový token (auth_invalid). Skontroluj HA_TOKEN v config.env.")
        raise HAError("Neočakávaná odpoveď pri overení (typ %s)." % _kind(msg))

    # ------------------------------------------------------------ príkazy
    def call(self, payload, timeout=None):
        """Pošle príkaz (dict bez `id`), vráti `result` odpovede s rovnakým id. Chyba = HAError."""
        if self._sock is None:
            raise HAError("Spojenie s Home Assistantom nie je otvorené.")
        cid = self._next_id
        self._next_id += 1
        msg = dict(payload)
        msg["id"] = cid
        limit = float(timeout) if timeout is not None else self._timeout
        deadline = time.monotonic() + limit
        self._sock.settimeout(limit)
        self._send_json(msg)
        while True:
            if time.monotonic() > deadline:
                raise HAError("Home Assistant neodpovedal na príkaz včas (limit %g s)." % limit)
            m = self._recv_json()
            if m.get("type") != "result" or m.get("id") != cid:
                continue            # udalosti a odpovede na iné id
            if m.get("success") is not True:
                err = m.get("error") or {}
                text = str(err.get("message") or err.get("code") or "neznáma chyba")
                if self._token:
                    text = text.replace(self._token, "***")
                raise HAError("Home Assistant vrátil chybu: %s" % text[:300])
            return m.get("result")

    # ------------------------------------------------------------ nízka úroveň
    def _send_raw(self, data):
        if self._sock is None:
            raise HAError("Spojenie s Home Assistantom nie je otvorené.")
        try:
            self._sock.sendall(data)
        except socket.timeout:
            raise HAError("Odoslanie do Home Assistanta vypršalo (časový limit).") from None
        except OSError as e:
            raise HAError("Odoslanie do Home Assistanta zlyhalo (%s)." % _why(e)) from None

    def _send_json(self, obj):
        self._send_raw(_frame(OP_TEXT, json.dumps(obj, ensure_ascii=False).encode("utf-8")))

    def _recv_more(self):
        if self._sock is None:
            raise HAError("Spojenie s Home Assistantom nie je otvorené.")
        try:
            chunk = self._sock.recv(65536)
        except socket.timeout:
            raise HAError("Home Assistant neodpovedá (vypršal časový limit).") from None
        except OSError as e:
            raise HAError("Čítanie z Home Assistanta zlyhalo (%s)." % _why(e)) from None
        if not chunk:
            raise HAError("Spojenie s Home Assistantom bolo ukončené.")
        self._buf += chunk

    def _need(self, n):
        while len(self._buf) < n:
            self._recv_more()
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _read_until(self, marker, limit):
        while marker not in self._buf:
            if len(self._buf) > limit:
                raise HAError("Odpoveď servera má príliš dlhé hlavičky.")
            self._recv_more()
        i = self._buf.index(marker) + len(marker)
        if i > limit:
            raise HAError("Odpoveď servera má príliš dlhé hlavičky.")
        out = bytes(self._buf[:i])
        del self._buf[:i]
        return out

    def _recv_text(self):
        """Ďalšia úplná dátová správa (bajty). Ping sa zodpovie pongom, pong sa ignoruje, close ukončí spojenie."""
        parts, total, fragmenting = [], 0, False
        while True:
            b0, b1 = self._need(2)
            if b0 & 0x70:
                raise HAError("Neplatný rámec z Home Assistanta (RSV bity).")
            fin = bool(b0 & 0x80)
            op = b0 & 0x0F
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack("!H", self._need(2))[0]
            elif ln == 127:
                ln = struct.unpack("!Q", self._need(8))[0]
                if ln >> 63:
                    raise HAError("Neplatná dĺžka rámca z Home Assistanta.")
            mask = self._need(4) if b1 & 0x80 else None
            if op >= 0x8 and (ln > 125 or not fin):
                raise HAError("Neplatný riadiaci rámec z Home Assistanta.")
            if ln > MAX_MSG or total + ln > MAX_MSG:
                raise HAError("Správa z Home Assistanta je príliš veľká (limit 64 MB).")
            payload = self._need(ln) if ln else b""
            if mask:
                payload = _xor(payload, mask)
            if op == OP_PING:
                self._send_raw(_frame(OP_PONG, payload))
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                self.close()
                raise HAError("Home Assistant ukončil spojenie (close).")
            if op in (OP_TEXT, OP_BIN):
                if fragmenting:
                    raise HAError("Neplatná fragmentácia správy (chýba pokračovací rámec).")
                fragmenting = not fin
            elif op == OP_CONT:
                if not fragmenting:
                    raise HAError("Neplatná fragmentácia správy (osamelý pokračovací rámec).")
                fragmenting = not fin
            else:
                raise HAError("Neznámy typ rámca z Home Assistanta (%d)." % op)
            parts.append(payload)
            total += ln
            if fin:
                return b"".join(parts)

    def _recv_json(self):
        raw = self._recv_text()
        try:
            msg = json.loads(raw.decode("utf-8"))
        except ValueError:          # vrátane UnicodeDecodeError
            raise HAError("Home Assistant poslal správu, ktorá nie je platný JSON.") from None
        if not isinstance(msg, dict):
            raise HAError("Home Assistant poslal správu neočakávaného tvaru.")
        return msg


def _kind(msg):
    return str(msg.get("type"))[:40]


# ---------------------------------------------------------------- spotreba celého domu (elektromer v HA)
# Hlavný elektromer: hodinové dáta distribučnej spoločnosti sa do HA importujú ako externá štatistika `ssd_ims:...`
# (denne, so oneskorením ~1 deň). Entity `..._high/low_tariff_meter_reading` majú v HA len zriedka sa meniace stavy
# (bez hodinového rozlíšenia), preto slúžia iba ako záloha, ak by externá štatistika chýbala.
# ID závisia od vašej inštalácie – nastavte ich ako premenné prostredia (napr. Environment= v systemd službe).
ENERGY_EXT_ID = os.environ.get("ENERGY_EXT_ID", "ssd_ims:odberne_miesto_actual_consumption")
ENERGY_METER_IDS = tuple(x.strip() for x in os.environ.get(
    "ENERGY_METER_IDS", "sensor.elektromer_high_tariff_meter_reading,sensor.elektromer_low_tariff_meter_reading").split(",") if x.strip())
MAX_HOUR_KWH = 60.0               # viac kWh za hodinu pre dom je určite chyba údajov


def _iso_utc(ts):
    import datetime
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()


def _stat_ts(v):
    """Čas štatistiky z HA (epoch ms, epoch s alebo ISO) -> unix sekundy (int) alebo None."""
    import datetime
    try:
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            x = float(v)
        else:
            s = str(v).strip()
            try:
                x = float(s)
            except ValueError:
                d = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
                if d.tzinfo is None:
                    d = d.replace(tzinfo=datetime.timezone.utc)
                return int(d.timestamp())
        return int(x / 1000.0 if abs(x) >= 1e11 else x)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _changes(rows):
    """{začiatok hodiny: prírastok kWh} z riadkov štatistiky (pole `change`)."""
    out = {}
    for r in rows or ():
        if not isinstance(r, dict):
            continue
        ts = _stat_ts(r.get("start"))
        ch = r.get("change")
        if ts is None or isinstance(ch, bool) or not isinstance(ch, (int, float)):
            continue
        out[ts - ts % 3600] = float(ch)
    return out


def energy_hourly(cli, a, b):
    """Hodinová spotreba celého domu v kWh za [a, b) (unix s) z HA: ({začiatok_hodiny: kWh}, info).
    Hodnoty <= 0 sa vynechajú (dom nikdy nespotrebuje presne 0; nula znamená stuhnutý zdroj). Iba čítanie."""
    a, b = int(a), int(b)
    ids = [ENERGY_EXT_ID] + list(ENERGY_METER_IDS)
    res = cli.call({"type": "recorder/statistics_during_period", "start_time": _iso_utc(a), "end_time": _iso_utc(b),
                    "statistic_ids": ids, "period": "hour", "types": ["change"]})
    if not isinstance(res, dict):
        raise HAError("Neočakávaný tvar odpovede na štatistiky elektromera.")

    def ok(d):
        return {h: v for h, v in d.items() if a - 3600 < h < b and 0 < v <= MAX_HOUR_KWH}
    ext = ok(_changes(res.get(ENERGY_EXT_ID)))
    if ext:
        return ext, {"zdroj": "ssd_ims", "hodin": len(ext)}
    hi, lo = _changes(res.get(ENERGY_METER_IDS[0])), _changes(res.get(ENERGY_METER_IDS[1]))
    tot = {h: hi.get(h, 0.0) + lo.get(h, 0.0) for h in set(hi) | set(lo)}
    tot = ok(tot)
    return tot, {"zdroj": "tarify" if tot else None, "hodin": len(tot)}
