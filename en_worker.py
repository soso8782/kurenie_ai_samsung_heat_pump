#!/usr/bin/env python3
"""Pomocný proces pre test_en_kompletne.py (nepúšťa sa samostatne v produkcii).

Postaví izolovanú inštaláciu (dočasné databázy, profil, mock ESP/OpenRouter/notify), prejde pevný scenár agenta
(hodinový beh, učenie, týždenný súhrn, chat, zásahy, poplachy) a vypíše JSON: všetko, čo by dostal model
(systémový prompt + vstup), a texty, ktoré agent ukladá / posiela. Čas je zafixovaný, časová zóna CET (bez letného času).

Použitie: python -X utf8 en_worker.py <výstup.json> <sk|en> <profil.json|-> [tmp_priečinok]"""
import json, os, sqlite3, sys, tempfile, time
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["TZ"] = "CET-1"
if hasattr(time, "tzset"):
    time.tzset()

OUT, LANG, PROFILE = sys.argv[1], sys.argv[2], sys.argv[3]
TMP = sys.argv[4] if len(sys.argv) > 4 else tempfile.mkdtemp()

import profil
if PROFILE != "-":
    profil.PROFILE_FILE = PROFILE
import common, agent, contacts, temps, abtest, interventions, notify

EN = LANG == "en"
MID = int(time.mktime((2026, 11, 15, 0, 0, 0, 0, 0, -1)))
NOW = MID + 19 * 3600                    # nedeľa 15. 11. 2026 19:00 (týždenný súhrn)
CLOCK = [NOW]
SEED = {
    "msg_admin": ["Closed the nursery valve by one turn, aired the living room." if EN else "Priškrtil som ventil v detskej o jednu otáčku, vetral som v obývačke.",
                  "We have guests until Sunday." if EN else "Do nedele máme hostí."],
    "msg_user": "It is cold in the bedroom." if EN else "V spálni je chladno.",
    "note_title": "Analysis: curve is sufficient" if EN else "Analýza: krivka postačuje",
    "note_body": "Outdoor 3 degrees, nursery on target." if EN else "Vonku 3 stupne, detská na cieli.",
    "lesson": ["At 0 °C outside the house uses about 40 kWh a day." if EN else "Pri 0 °C vonku dom spotrebuje okolo 40 kWh denne."],
    "lesson_sum": "Stable day." if EN else "Stabilný deň.",
    "iv_pending": "Throttled valve in the bedroom 6->5" if EN else "Priškrtený ventil v spálni 6->5",
    "iv_done": "Opened the return valve by 1 turn" if EN else "Pootvorený spätný ventil o 1 otáčku",
    "iv_verdict": "The nursery is 0.3 degrees warmer." if EN else "Detská je o 0,3 stupňa teplejšia.",
    "act_reason": "Curve for 3 days below target" if EN else "Krivka 3 dni pod cieľom",
    "chyba_txt": "Nenulový chybový kód TČ",          # texty zberača uložené v DB z minulosti (po slovensky)
    "start_txt": "START (pauza 12 min) - vonku 3.2 °C, Tw2 40.0 °C, Tw1 35.0 °C, WL 38.0 °C, ventil Heating, ODU Heating, termostat ON, odmraz. OFF",
    "stop_txt": "STOP (chod 40 min, max 45 Hz) - vonku 3.1 °C, Tw2 41.0 °C, Tw1 36.0 °C, WL 38.0 °C, ventil Heating, ODU Idle, termostat OFF, odmraz. ON",
    "alias_win": "Bedroom window" if EN else "Okno spálňa",
    "alias_door": "Garage door" if EN else "Garážové dvere",
    "alias_off": "Terrace door" if EN else "Terasa dvere",
    "t_room": "Bedroom thermometer" if EN else "Teplomer spálňa",
    "t_rad": "Radiator head bedroom" if EN else "Hlavica radiátora spálňa",
    "t_off": "Boiler room thermometer" if EN else "Teplomer kotolňa",
    "t_note": "behind the curtain" if EN else "za závesom",
}

cfg = {"AGENT_DB": os.path.join(TMP, "a.db"), "TC_DB": os.path.join(TMP, "tc.db"), "STATS_DB": os.path.join(TMP, "stats.db"),
       "OPENROUTER_API_KEY": "k", "OPENROUTER_MODEL": "m", "OPENROUTER_FALLBACK_MODEL": "", "UI_LANG": LANG, "DRY_RUN": "1",
       "HA_URL": "http://ha.invalid:8123", "HA_TOKEN": "tok-abcdefghijklmnop", "ESP_URL": "http://esp.invalid",
       "LAT": "48.7", "LON": "19.1", "INTERVAL_MIN": "60", "LLM_MAX_GAP_H": "4", "RETENTION_FULL_DAYS": "60"}

S, ROOMS, SETTINGS = common.S, common.ROOMS, common.SETTINGS


def seed_tc():
    tc = sqlite3.connect(cfg["TC_DB"])
    tc.executescript("""CREATE TABLE IF NOT EXISTS samples(ts INTEGER, entity TEXT, value REAL);
    CREATE TABLE IF NOT EXISTS events(ts INTEGER, kind TEXT, entity TEXT, old TEXT, new TEXT, text TEXT);
    CREATE TABLE IF NOT EXISTS notes(ts INTEGER, author TEXT, title TEXT, body TEXT);
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);""")
    rows = []
    start = MID - 12 * 86400
    t = start
    counter = 0.0
    sets = {"fsv2011": -10.0, "fsv2012": 15.0, "fsv2021": 35.0, "fsv2022": 28.0, "fsv2031": 45.0, "fsv2032": 35.0, "fsv4013": 20.0,
            "max_posun": 3.0, "max_posun_zaporny": 2.0, "zosilnenie": 1.0}
    while t < NOW - 600:
        i = (t - start) // 1800
        vonku = 3.0 + 4.0 * ((i % 48) / 48.0 - 0.5)
        run = (i % 3) != 0
        vals = {"vonku": vonku, "ciel": 23.5, "wl": 38.0 + (i % 5), "tw2": 40.0 + (i % 4), "tw1": 35.5, "dt": 4.8 + (0.4 if i % 17 == 0 else 0),
                "freq": 40.0 if run else 0.0, "posun": 1.5 + (i % 4) * 0.5, "bojler": 44.0, "prikon": 1.2}
        for k, v in vals.items():
            if k in S:
                rows.append((t, S[k], v))
        for j, r in enumerate(ROOMS):
            if r in S:
                rows.append((t, S[r], 23.2 - 0.15 * (i % 3) - 0.9 * j))
        if "spotreba" in S:
            counter += 0.3
            if (t - start) % 86400 < 1800:
                counter = 0.3
            rows.append((t, S["spotreba"], counter))
        for k, nme in SETTINGS.items():
            rows.append((t, nme, sets.get(k, 1.0)))
        t += 1800
    t = NOW - 1200
    while t <= NOW - 60:
        if REF in S:
            rows.append((t, S[REF], 21.0))
        if "ciel" in S:
            rows.append((t, S["ciel"], 23.5))
        for r in ROOMS[1:]:
            if r in S:
                rows.append((t, S[r], 18.5 if r == ROOMS[1] else 22.5))
        t += 60
    tc.executemany("INSERT INTO samples VALUES(?,?,?)", rows)
    ev = [(NOW - 5 * 3600, "chyba", S.get("chyba", "E"), "0", "911", SEED["chyba_txt"]),
          (NOW - 4 * 3600, "kompresor", "Kompresor", None, None, SEED["start_txt"]),
          (NOW - 3 * 3600, "ovladanie", "Heating", "ON", "OFF", None),
          (NOW - 3 * 3600 + 600, "kompresor", "Kompresor", None, None, SEED["stop_txt"]),
          (NOW - 5400, "system", "ESP", None, None, "ESP nedostupné (URLError)"),
          (NOW - 5000, "system", "ESP", None, None, "ESP znova dostupné"),
          (NOW - 5300, "system", "zberač", None, None, "Zberač spustený"),
          (NOW - 2 * 3600, "nastavenie", SETTINGS["max_posun"], "2.5", "3.0", None),
          (NOW - 2 * 86400, "nastavenie", SETTINGS["fsv2031"], "44.0", "45.0", None),
          (NOW - 3600, "system", "ESP", None, None, "x"),
          (NOW - 1800, "tarifa", "tarifa", None, None, "[I] tarifa test")]
    tc.executemany("INSERT INTO events VALUES(?,?,?,?,?,?)", ev)
    tc.execute("INSERT INTO notes VALUES(?,?,?,?)", (NOW - 7200, "agent", SEED["note_title"], SEED["note_body"]))
    tc.execute("INSERT OR REPLACE INTO meta VALUES('last_sample',?)", (str(NOW - 60),))
    tc.commit()
    tc.close()


REF = common.REF_ROOM


def seed_agent(c):
    agent.update_days(cfg, c)
    for k, v in (("mode", "control"), ("season", "zima")):
        common.set_state(c, k, v)
    ins = lambda sql, a: c.execute(sql, a)
    ins("INSERT INTO messages(ts,role,text,author) VALUES(?,?,?,?)", (NOW - 20 * 3600, "user", SEED["msg_admin"][0], None))
    ins("INSERT INTO messages(ts,role,text,author) VALUES(?,?,?,?)", (NOW - 19 * 3600, "agent", "Noted." if EN else "Zapísané.", None))
    ins("INSERT INTO messages(ts,role,text,author) VALUES(?,?,?,?)", (NOW - 2 * 3600, "user", "[u@x.sk] " + SEED["msg_user"], "u@x.sk"))
    ins("INSERT INTO messages(ts,role,text,author) VALUES(?,?,?,?)", (NOW - 3600, "user", SEED["msg_admin"][1], None))
    ins("INSERT INTO lessons(ts,date,lessons,summary,status,error,cost) VALUES(?,?,?,?,?,?,?)",
        (NOW - 86400, "2026-11-14", json.dumps(SEED["lesson"], ensure_ascii=False), SEED["lesson_sum"], "ok", None, 0.01))
    ins("INSERT INTO runs(ts,mode,status,stav,summary,model,cost) VALUES(?,?,?,?,?,?,?)", (NOW - 3 * 3600, "control", "ok", "ok", "Everything fine." if EN else "Všetko v poriadku.", "m", 0.01))
    ins("INSERT INTO actions(ts,entity,old,new,reason,applied,result) VALUES(?,?,?,?,?,?,?)",
        (NOW - 3 * 86400, SETTINGS["max_posun"], 2.5, 3.0, SEED["act_reason"], 1, "zapísané a overené"))
    ins("INSERT INTO actions(ts,entity,old,new,reason,applied,result) VALUES(?,?,?,?,?,?,?)",
        (NOW - 5 * 86400, SETTINGS["fsv2032"], 35.0, 36.0, SEED["act_reason"], 0, "odmietnuté: limit"))
    c.commit()
    interventions.add(c, "user_message", "ventil", ROOMS[2], SEED["iv_pending"], ts=NOW - 3 * 3600)
    iid = interventions.add(c, "user_manual", "ventil", ROOMS[1], SEED["iv_done"], ts=NOW - 4 * 86400)
    c.execute("UPDATE interventions SET status='hotove', result=? WHERE id=?", (json.dumps({"verdict_text": SEED["iv_verdict"]}, ensure_ascii=False), iid))
    c.commit()
    # okná a dvere
    contacts.ensure(c)
    gs = contacts.groups(c)
    g_room = [g for g in gs if g.get("room") == ROOMS[2]][0]["id"]
    g_other = contacts.other_id(c)
    cs = [("binary_sensor.t_okno", "okno", SEED["alias_win"], g_room, 1, "window", "on", NOW - 1800),
          ("binary_sensor.t_garaz", "garaz", SEED["alias_door"], g_other, 1, "door", "off", NOW - 7200),
          ("binary_sensor.t_terasa", "terasa", SEED["alias_off"], g_other, 1, "door", "unavailable", NOW - 7200)]
    for eid, hn, al, gid, en, dc, st, since in cs:
        c.execute("INSERT OR REPLACE INTO contacts(entity_id,ha_name,alias,group_id,enabled,device_class,state,since,area,note,first_seen,last_seen) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (eid, hn, al, gid, en, dc, st, since, None, None, NOW - 9 * 86400, NOW))
    evs = [(NOW - 30 * 86400, "binary_sensor.t_okno", "off"), (NOW - 5 * 3600, "binary_sensor.t_okno", "on"), (NOW - 4 * 3600, "binary_sensor.t_okno", "off"),
           (NOW - 1800, "binary_sensor.t_okno", "on"), (NOW - 20 * 3600, "binary_sensor.t_garaz", "on"), (NOW - 19 * 3600, "binary_sensor.t_garaz", "off")]
    c.executemany("INSERT INTO contact_events(ts,entity_id,state) VALUES(?,?,?)", evs)
    contacts._set_ha_state(c, True, now=NOW, count=3)
    # teplotné čidlá
    ts_ = [("sensor.t_izba", "izba", SEED["t_room"], g_room, 1, "izba", 21.5, NOW - 100, SEED["t_note"]),
           ("sensor.t_rad", "rad", SEED["t_rad"], g_room, 1, "radiator", 33.0, NOW - 100, None),
           ("sensor.t_kot", "kot", SEED["t_off"], g_other, 1, "technicke", None, NOW - 7200, None)]
    for eid, hn, al, gid, en, kind, val, upd, note in ts_:
        c.execute("INSERT OR REPLACE INTO temp_sensors(entity_id,ha_name,alias,group_id,enabled,kind,value,updated,area,note,alert_min,alert_max,first_seen,last_seen) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (eid, hn, al, gid, en, kind, val, upd, None, note, None, None, NOW - 9 * 86400, NOW))
    t = MID - 3 * 86400
    while t < NOW:
        c.execute("INSERT INTO temp_samples VALUES(?,?,?)", (t, "sensor.t_izba", 21.0 + (t % 7200) / 7200.0))
        c.execute("INSERT INTO temp_samples VALUES(?,?,?)", (t, "sensor.t_rad", 31.0 + (t % 3600) / 900.0))
        t += 3600
    temps._set_poll_state(c, True, now=NOW, count=3)
    c.commit()


def abtest_on(c):
    common.set_state(c, "abtest", json.dumps({"status": "running", "start": "2026-11-14", "days": [
        {"date": "2026-11-14", "mode": "A", "valid": True}, {"date": "2026-11-15", "mode": "B", "valid": True}, {"date": "2026-11-16", "mode": "A"}]}))
    c.commit()


def abtest_off(c):
    common.set_state(c, "abtest", "")
    c.commit()


# ---------------------------------------------------------------- mocky
CALLS, RES, NOTIFS = [], {}, []
LABEL = ["?"]
FORECAST = {"hourly": {"time": ["2026-11-15T19:00", "2026-11-15T20:00", "2026-11-15T21:00", "2026-11-15T22:00"],
                       "temperature_2m": [3.1, 2.5, 2.0, 1.8], "cloud_cover": [80, 70, 60, 50], "shortwave_radiation": [0, 0, 0, 12.4]}}


class _Resp:
    def __init__(self, body):
        self.body = body
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


def fake_urlopen(req, timeout=None):
    url = req if isinstance(req, str) else req.full_url
    if "open-meteo" in url:
        return _Resp(json.dumps(FORECAST).encode("utf-8"))
    raise OSError("blocked: " + url)


def fake_llm(cfg_, system, user, max_tokens=3000, as_json=True, validate=None, info=None):
    CALLS.append({"label": LABEL[0], "system": system, "user": user})
    if info is not None:
        info["model"] = "m"
    kind = LABEL[0].split(":")[0]
    if kind == "run":
        ent = [k for k in agent.ALLOWED]
        a = ACTS[LABEL[0]]
        d = {"stav": "pozor", "hodnotenie": "Assessment." if EN else "Hodnotenie.", "akcie": a, "odporucanie": "Open the valve." if EN else "Otvor ventil."}
    elif kind == "learn":
        d = {"zhrnutie_dna": "Calm day." if EN else "Pokojný deň.", "poucenia": SEED["lesson"], "sledovat": "Valves." if EN else "Ventily."}
    elif kind == "weekly":
        d = {"suhrn": "The week was calm. Consumption fell." if EN else "Týždeň bol pokojný. Spotreba klesla.", "odporucania": ["Keep it." if EN else "Ponechať."]}
    elif kind == "chat_user":
        d = {"odpoved": "Passing it on." if EN else "Odovzdávam.", "ciel": 23.0}
    else:
        d = {"odpoved": "Noted, waiting 24 hours." if EN else "Zapísané, počkám 24 hodín.", "zasah": {"typ": "ventil", "izba": ROOMS[1], "popis": SEED["iv_pending"], "cas": None}}
    return json.dumps(d, ensure_ascii=False), 10, 5, 0.001


ACTS = {}
ESP_VALS = {}


def fake_esp_get(cfg_, domain, name, timeout=6):
    return {"value": ESP_VALS.get(name)}


def fake_notify(cfg_, kind, title, msg, *a, **k):
    NOTIFS.append({"kind": kind, "title": title, "message": msg})
    return True, None


def cur_values():
    cur = {}
    for dom, name in agent.READ_NOW:
        cur[name] = None
    for n, (mn, mx, step, hrs, q) in agent.ALLOWED.items():
        cur[n] = round((mn + mx) / 2 / q) * q
    cur[S["vonku"]] = 3.2
    cur[S[REF]] = 21.0
    if "ciel" in S:
        cur[S["ciel"]] = 23.5
    cur[agent.dhw.ENT_TARGET] = 50.0 if hasattr(agent.dhw, "ENT_TARGET") else None
    if agent.E_CHYBA:
        cur[agent.E_CHYBA] = 0.0
    cur["Elektrický dohrev kúrenia (Backup heater)"] = True
    ESP_VALS.update({k: v for k, v in cur.items() if isinstance(v, (int, float))})
    return cur


def main():
    seed_tc()
    c = common.adb(cfg)
    seed_agent(c)
    out = {"consts": {k: getattr(agent, k) for k in ("SYSTEM", "LEARN_SYSTEM", "CHAT_SYSTEM", "CHAT_SYSTEM_USER", "WEEKLY_SYSTEM")}}
    cur = cur_values()
    names = list(agent.ALLOWED)
    n_a = [n for n in names if n == SETTINGS["fsv4013"]][0]
    n_c, n_f, n_m = SETTINGS["fsv2031"], SETTINGS["fsv2032"], SETTINGS["max_posun"]
    cur_all = lambda: dict(cur)
    now = float(NOW)

    patches = [mock.patch("time.time", lambda: CLOCK[0]), mock.patch("time.sleep", lambda s: None),
               mock.patch.object(agent, "call_llm", fake_llm), mock.patch.object(agent, "esp_get", fake_esp_get),
               mock.patch.object(notify, "event", fake_notify), mock.patch("urllib.request.urlopen", fake_urlopen)]
    for p in patches:
        p.start()
    try:
        # 1) rozhodnutie, či volať model (test A/B beží)
        abtest_on(c)
        chk, hrs_now = [], None
        b = int(now) - int(now) % 3600 + 3600
        hrs_now = common.hour_stats(cfg, b - 24 * 3600, b)
        need, reasons = agent.needs_llm(cfg, c, cur_all(), hrs_now, True, chk, now)
        RES["needs_ab"] = {"need": need, "reasons": reasons, "checks": chk}
        # 2) beh s modelom počas testu A/B (všetko sa odmietne)
        ACTS["run:ab"] = [{"entita": n_a, "hodnota": cur[n_a] + 5, "dovod": SEED["act_reason"]}]
        LABEL[0] = "run:ab"
        rec = agent.run_once(cfg, c, "control", False, reasons=reasons, pre=(cur_all(), 0))
        RES["run:ab"] = {"actions": json.loads(rec["actions"]), "status": rec["status"], "error": rec["error"], "stav": rec["stav"]}
        # chat používateľa počas testu
        LABEL[0] = "chat_user:ab"
        r = agent.chat_reply(cfg, c, "I want 23 in the nursery" if EN else "V detskej chcem 23", role="user", email="u@x.sk")
        RES["chat_user:ab"] = list(r[:1])
        abtest_off(c)
        # 3) bežný beh: obmedzenia a čakajúci zásah
        chk2 = []
        need, reasons = agent.needs_llm(cfg, c, cur_all(), hrs_now, False, chk2, now)
        RES["needs"] = {"need": need, "reasons": reasons, "checks": chk2}
        ACTS["run:1"] = [{"entita": n_a, "hodnota": cur[n_a] + 5, "dovod": SEED["act_reason"]},
                         {"entita": n_c, "hodnota": cur[n_c] + 1, "dovod": SEED["act_reason"]},
                         {"entita": "Unknown entity", "hodnota": 1, "dovod": "x"},
                         {"entita": n_m, "hodnota": "abc", "dovod": "x"}]
        LABEL[0] = "run:1"
        rec = agent.run_once(cfg, c, "control", False, reasons=reasons, pre=(cur_all(), 0))
        RES["run:1"] = {"actions": json.loads(rec["actions"]), "status": rec["status"], "error": rec["error"], "stav": rec["stav"]}
        c.execute("UPDATE interventions SET status='zrusene' WHERE status='caka'")
        c.commit()
        # 4) otvorené okno v zime
        ACTS["run:2"] = [{"entita": n_c, "hodnota": cur[n_c] + 1, "dovod": SEED["act_reason"]}]
        LABEL[0] = "run:2"
        rec = agent.run_once(cfg, c, "control", False, reasons=["x"], pre=(cur_all(), 0))
        RES["run:2"] = {"actions": json.loads(rec["actions"])}
        # 5) leto + režim radí
        common.set_state(c, "season", "leto"); c.commit()
        ACTS["run:3"] = [{"entita": n_c, "hodnota": cur[n_c] + 1, "dovod": SEED["act_reason"]}, {"entita": n_a, "hodnota": cur[n_a] + 1, "dovod": "x"}]
        LABEL[0] = "run:3"
        rec = agent.run_once(cfg, c, "advise", False, reasons=["x"], pre=(cur_all(), 0))
        RES["run:3"] = {"actions": json.loads(rec["actions"])}
        input_summer = [x for x in CALLS if x["label"] == "run:3"][0]["user"]
        # 6) zima, zatvorené okná: zablokované + viac zmien než je povolené
        common.set_state(c, "season", "zima"); c.execute("DELETE FROM contact_events WHERE ts>?", (NOW - 86400,))
        c.execute("UPDATE contacts SET state='off'"); c.commit()
        ACTS["run:4"] = [{"entita": n_c, "hodnota": cur[n_c] + 1, "dovod": SEED["act_reason"]}]
        LABEL[0] = "run:4"
        rec = agent.run_once(cfg, c, "control", False, reasons=["x"], pre=(cur_all(), 6))
        RES["run:4"] = {"actions": json.loads(rec["actions"])}
        ACTS["run:5"] = [{"entita": n_c, "hodnota": cur[n_c] + 1, "dovod": SEED["act_reason"]},
                         {"entita": n_f, "hodnota": cur[n_f] + 1, "dovod": SEED["act_reason"]},
                         {"entita": n_m, "hodnota": cur[n_m] + 0.5, "dovod": SEED["act_reason"]}]
        LABEL[0] = "run:5"
        rec = agent.run_once(cfg, c, "control", False, reasons=["x"], pre=(cur_all(), 0))
        RES["run:5"] = {"actions": json.loads(rec["actions"])}
        # 7) beh bez kľúča
        cfg2 = dict(cfg, OPENROUTER_API_KEY="")
        rec = agent.run_once(cfg2, c, "advise", False)
        RES["run:nokey"] = {"status": rec["status"], "error": rec["error"]}
        # 8) vrátenie zmeny
        aid = c.execute("SELECT rowid FROM actions WHERE entity=? AND applied=1 ORDER BY ts DESC, rowid DESC LIMIT 1", (n_c,)).fetchone()[0]
        for k, v in list(ESP_VALS.items()):
            pass
        ESP_VALS[n_c] = cur[n_c] + 1
        RES["undo"] = list(agent.undo(cfg, c, aid, LANG))
        # 9) učenie a týždenný súhrn
        LABEL[0] = "learn"
        RES["learn"] = agent.learn(cfg, c)
        LABEL[0] = "weekly"
        RES["weekly"] = agent.weekly(cfg, c, now)
        # 10) chat správcu a používateľa
        LABEL[0] = "chat_admin"
        r = agent.chat_reply(cfg, c, "Closed the valve in the bedroom." if EN else "Zavrel som ventil v spálni.", role="admin")
        RES["chat_admin"] = list(r[:1])
        LABEL[0] = "chat_user:2"
        r = agent.chat_reply(cfg, c, "I want 23 in the nursery" if EN else "V detskej chcem 23", role="user", email="u@x.sk")
        RES["chat_user:2"] = list(r[:1])
        # 11) udalosť -> okamžitý beh
        ev_alerts = {"chyba_tc": agent.tr("srv.agent.chyba_tc", LANG, code=911, tail=agent.tr("srv.agent.nizky_prietok", LANG))}
        reason = agent.event_trigger(cfg, c, ev_alerts, set(), now)
        RES["event_reason"] = reason
        RES["room_conditions"] = agent.room_conditions(cfg, c, now)
        ACTS["run:6"] = []
        LABEL[0] = "run:6"
        with mock.patch.object(agent, "read_now", lambda cfg_: (cur_all(), 0)):
            rec = agent.cycle(cfg, c, "control", forced=True, event=reason)
        RES["cycle"] = {"status": rec["status"], "error": rec["error"]}
        # 12) bez modelu: rutina
        RES["rutina"] = agent.log_rutina(cfg, c, "advise", cur_all(), hrs_now, chk2, now)["summary"]
        out["input_summer"] = input_summer
    finally:
        for p in patches:
            p.stop()
    out["calls"] = CALLS
    out["results"] = RES
    out["notifications"] = NOTIFS
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, sort_keys=True)


if __name__ == "__main__":
    main()
