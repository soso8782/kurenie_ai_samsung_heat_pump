import json, os, socket, struct, sys, tempfile, threading, time, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import contacts, contacts_mqtt as cm
from common import adb, get_state


class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cfg = {"AGENT_DB": os.path.join(self.d, "a.db"), "TC_DB": os.path.join(self.d, "t.db")}
        self.c = adb(self.cfg)
        contacts.OVERRIDE.clear()
        items = [{"id": "binary_sensor.jedalen_okno_kontakt_sensor_contact", "name": "Jedalen okno", "dc": "door", "state": "on", "since": "2026-11-13T17:20:02+00:00"},
                 {"id": "binary_sensor.spalna_okno_kontakt_cidlo_contact", "name": "Spalna okno", "dc": "door", "state": "off", "since": "2026-11-13T05:52:00+00:00"}]
        contacts.apply_items(self.c, items, 1794590000)
        self.m = cm.Mapper()

    def st(self, eid):
        return self.c.execute("SELECT state FROM contacts WHERE entity_id=?", (eid,)).fetchone()[0]

    def test_slug(self):
        self.assertEqual(cm.slug("Spálňa_okno_kontakt_čidlo"), "spalna_okno_kontakt_cidlo")
        self.assertEqual(cm.slug("0x00158d000272a9d3"), "0x00158d000272a9d3")

    def test_contact_state(self):
        self.assertEqual(cm.contact_state(b'{"contact":true,"battery":100}'), "off")
        self.assertEqual(cm.contact_state(b'{"contact":false}'), "on")
        self.assertIsNone(cm.contact_state(b'{"temperature":20}'))
        self.assertIsNone(cm.contact_state(b'online'))

    def test_handle_closes_window(self):
        eid = cm.handle(self.c, self.m, "zigbee2mqtt", "zigbee2mqtt/Jedalen_okno_kontakt_sensor", b'{"contact":true}', False, 1794592000)
        self.assertEqual(eid, "binary_sensor.jedalen_okno_kontakt_sensor_contact")
        self.assertEqual(self.st(eid), "off")
        # opakovaná správa nič nemení
        self.assertIsNone(cm.handle(self.c, self.m, "zigbee2mqtt", "zigbee2mqtt/Jedalen_okno_kontakt_sensor", b'{"contact":true}', False, 1794592001))

    def test_ignored(self):
        h = lambda t, p, r=False: cm.handle(self.c, self.m, "zigbee2mqtt", t, p, r, 1794592000)
        self.assertIsNone(h("zigbee2mqtt/Jedalen_okno_kontakt_sensor", b'{"contact":true}', True))   # retained
        self.assertIsNone(h("zigbee2mqtt/Jedalen_okno_kontakt_sensor/availability", b'{"contact":true}'))
        self.assertIsNone(h("zigbee2mqtt/Neznamy", b'{"contact":true}'))
        self.assertIsNone(h("iny/Jedalen_okno_kontakt_sensor", b'{"contact":true}'))

    def test_override_beats_stale_ha(self):
        eid = "binary_sensor.jedalen_okno_kontakt_sensor_contact"
        cm.handle(self.c, self.m, "zigbee2mqtt", "zigbee2mqtt/Jedalen_okno_kontakt_sensor", b'{"contact":true}', False, 1794592000)
        stale = [{"id": eid, "name": "x", "dc": "door", "state": "on", "since": "2026-11-13T17:20:02+00:00"}]
        contacts.apply_items(self.c, stale, 1794592010)
        self.assertEqual(self.st(eid), "off")                  # HA ešte zaostáva, platí MQTT
        fresh = [{"id": eid, "name": "x", "dc": "door", "state": "on", "since": "2030-01-01T00:00:00+00:00"}]
        contacts.apply_items(self.c, fresh, 1794592020)
        self.assertEqual(self.st(eid), "on")                   # HA je novšie, platí HA

    def test_packets(self):
        p = cm.build_connect("id", "u", "p")
        self.assertEqual(p[0], 0x10)
        self.assertEqual(cm.build_subscribe(1, "a/+")[0], 0x82)
        topic, pl, ret = cm.parse_publish(0, b"\x00\x03a/b{}")
        self.assertEqual((topic, pl, ret), ("a/b", b"{}", False))


class StateT(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cfg = {"AGENT_DB": os.path.join(self.d, "a.db"), "TC_DB": os.path.join(self.d, "t.db")}
        self.c = adb(self.cfg); self.cfg = self.cfg
        cm._wr.clear(); cm.matched.clear(); contacts.OVERRIDE.clear()
        contacts.apply_items(self.c, [{"id": "binary_sensor.jedalen_okno_kontakt_sensor_contact", "name": "Jedalen okno", "dc": "door", "state": "on", "since": "2026-11-13T17:20:02+00:00"}], 1794590000)

    def test_run_once_reload_and_state(self):
        b = FakeBroker(0, [])
        cfg = dict(self.cfg, MQTT_HOST="127.0.0.1", MQTT_PORT=str(b.port), MQTT_BASE="zigbee2mqtt")
        threading.Timer(1.0, lambda: (lambda c: (self.app_set(c), c.close()))(adb(cfg))).start()
        with mock.patch.object(cm, "READ_TIMEOUT", 0.3), mock.patch.object(cm, "RELOAD_CHECK", 0.2):
            t0 = time.time(); cm.run_once(cfg)
        self.assertLess(time.time() - t0, 3.5)                                       # skončilo kvôli signálu, nie chybou
        self.assertEqual(get_state(self.c, "mqtt_connected"), "0")

    def app_set(self, c):
        from common import set_state
        set_state(c, "mqtt_reload", "novy")

    def test_put_state_throttle(self):
        self.assertTrue(cm.put_state(self.c, "mqtt_last_msg", 100, 10, 1000.0))
        self.assertFalse(cm.put_state(self.c, "mqtt_last_msg", 101, 10, 1005.0))     # príliš skoro
        self.assertTrue(cm.put_state(self.c, "mqtt_last_msg", 102, 10, 1011.0))
        self.assertEqual(get_state(self.c, "mqtt_last_msg"), "102")
        self.assertTrue(cm.put_state(self.c, "mqtt_connected", "1"))
        self.assertFalse(cm.put_state(self.c, "mqtt_connected", "1"))                # bez zmeny sa nezapisuje
        self.assertTrue(cm.put_state(self.c, "mqtt_connected", "0"))                 # zmena hneď
        self.assertEqual(get_state(self.c, "mqtt_connected"), "0")

    def test_handle_writes_matched_once(self):
        m = cm.Mapper()
        cm.handle(self.c, m, "zigbee2mqtt", "zigbee2mqtt/Jedalen_okno_kontakt_sensor", b'{"contact":true}', False, 1794592000)
        self.assertEqual(json.loads(get_state(self.c, "mqtt_matched")), ["binary_sensor.jedalen_okno_kontakt_sensor_contact"])
        self.assertEqual(get_state(self.c, "mqtt_last_contact"), "1794592000")
        self.c.execute("UPDATE state SET v='[]' WHERE k='mqtt_matched'"); self.c.commit()
        cm.handle(self.c, m, "zigbee2mqtt", "zigbee2mqtt/Jedalen_okno_kontakt_sensor", b'{"contact":false}', False, 1794592100)
        self.assertEqual(get_state(self.c, "mqtt_matched"), "[]")                    # množina sa nezmenila -> bez zápisu


class FakeBroker:
    """Minimálny broker na localhost: CONNACK s daným kódom, potom pošle PUBLISH správy."""
    def __init__(self, rc=0, topics=()):
        self.srv = socket.socket(); self.srv.bind(("127.0.0.1", 0)); self.srv.listen(1)
        self.port = self.srv.getsockname()[1]; self.rc, self.topics, self.got = rc, topics, b""
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        s, _ = self.srv.accept()
        try:
            t, _, body = cm.read_packet(s)
            self.got = body
            s.sendall(b"\x20\x02\x00" + bytes([self.rc]))
            if self.rc:
                return
            cm.read_packet(s)    # SUBSCRIBE
            for tp in self.topics:
                b = tp.encode(); pl = b'{"contact":true}'
                s.sendall(b"\x30" + bytes([2 + len(b) + len(pl)]) + struct.pack(">H", len(b)) + b + pl)
            time.sleep(4)
        except Exception:
            pass
        finally:
            s.close(); self.srv.close()


class AppT(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import app
        except Exception as e:
            raise unittest.SkipTest("app.py sa nedá importovať: %r" % (e,))
        cls.app = app

    def v(self, **kw):
        b = {"host": "192.168.1.10", "port": 1883, "user": "z2m", "base": "zigbee2mqtt"}; b.update(kw)
        return self.app.mqtt_validate(b, {"MQTT_PASS": "stare"})

    def test_validate_ok_keeps_password(self):
        err, v = self.v()
        self.assertIsNone(err); self.assertEqual((v["pass"], v["new_pass"]), ("stare", False))
        err, v = self.v(**{"pass": "nove"}); self.assertEqual((v["pass"], v["new_pass"]), ("nove", True))
        self.assertEqual(self.v(port="", base="")[1]["port"], 1883)

    def test_validate_rejects(self):
        for kw in ({"host": ""}, {"host": "a b"}, {"host": "x" * 254}, {"host": "-bad"}, {"port": 0}, {"port": 70000}, {"port": "abc"},
                   {"user": "a b"}, {"user": "a;b"}, {"base": "/z2m"}, {"base": "z2m/"}, {"base": "a//b"}, {"base": "a b"},
                   {"pass": "x\ny"}, {"pass": " x"}, {"pass": '"x"'}):
            self.assertIsNotNone(self.v(**kw)[0], kw)
        self.assertIsNone(self.v(host="broker.local", base="home/z2m")[0])

    def test_post_saves_and_signals(self):
        d = tempfile.mkdtemp(); cfg = {"AGENT_DB": os.path.join(d, "a.db"), "TC_DB": ""}; c = adb(cfg)
        with mock.patch.object(self.app, "save_cfg") as sc:
            code, out = self.app.mqtt_post(cfg, c, {"host": "10.0.0.5", "port": "1884", "user": "u", "base": "z2m", "pass": ""})
            self.assertEqual((code, out), (200, {"ok": True}))
            upd = sc.call_args[0][0]
            self.assertEqual(upd, {"MQTT_HOST": "10.0.0.5", "MQTT_PORT": "1884", "MQTT_USER": "u", "MQTT_BASE": "z2m"})   # prázdne heslo = bez zmeny
            self.assertIsNotNone(get_state(c, "mqtt_reload"))
            self.app.mqtt_post(cfg, c, {"host": "10.0.0.5", "port": 1884, "user": "u", "base": "z2m", "pass": "tajne"})
            self.assertEqual(sc.call_args[0][0]["MQTT_PASS"], "tajne")
            code, out = self.app.mqtt_post(cfg, c, {"host": "", "port": 1})
            self.assertEqual(code, 400)

    def test_get_hides_password(self):
        d = tempfile.mkdtemp(); cfg = {"AGENT_DB": os.path.join(d, "a.db"), "TC_DB": "", "MQTT_HOST": "h", "MQTT_PASS": "tajne"}; c = adb(cfg)
        contacts.apply_items(c, [{"id": "binary_sensor.a_contact", "name": "Okno A", "dc": "door", "state": "on", "since": "2026-11-13T17:20:02+00:00"}], 1794590000)
        cm_state = {"mqtt_connected": "1", "mqtt_last_msg": "1794592000", "mqtt_matched": json.dumps(["binary_sensor.a_contact", "binary_sensor.zmazany_contact"])}
        for k, v in cm_state.items():
            self.app.set_state(c, k, v)
        out = self.app.mqtt_get(cfg, c)
        self.assertNotIn("tajne", json.dumps(out)); self.assertTrue(out["pass_set"])
        self.assertTrue(out["status"]["connected"]); self.assertIsNone(out["status"]["last_contact"])
        self.assertEqual(out["status"]["matched"], [{"entity_id": "binary_sensor.a_contact", "label": "Okno A"}])

    def test_mqtt_test_ok_and_errors(self):
        b = FakeBroker(0, ["zigbee2mqtt/A", "zigbee2mqtt/B", "zigbee2mqtt/A"])
        r = self.app.mqtt_test({"MQTT_PASS": "tajne"}, {"host": "127.0.0.1", "port": b.port, "user": "u", "base": "zigbee2mqtt"})
        self.assertTrue(r["ok"]); self.assertIn("2 zariadení", r["text"]); self.assertNotIn("tajne", r["text"])
        self.assertIn(b"tajne", b.got)                                               # prázdne pole = použité uložené heslo
        b = FakeBroker(4)
        r = self.app.mqtt_test({}, {"host": "127.0.0.1", "port": b.port, "user": "u", "pass": "zle", "base": "zigbee2mqtt"})
        self.assertFalse(r["ok"]); self.assertIn("heslo", r["text"]); self.assertNotIn("zle", r["text"])
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        r = self.app.mqtt_test({}, {"host": "127.0.0.1", "port": port})
        self.assertFalse(r["ok"])

if __name__ == "__main__":
    unittest.main()
