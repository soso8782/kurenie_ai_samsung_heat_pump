#!/usr/bin/env python3
"""Import hodinových štatistík z HA: riadky entity_id;YYYY-MM-DD HH;mean;min;max -> tabuľka hourly."""
import sqlite3, sys, time, datetime

c = sqlite3.connect("/opt/tc-monitor/tc.db", timeout=30)
c.execute("CREATE TABLE IF NOT EXISTS hourly(entity TEXT, ts INTEGER, mean REAL, min REAL, max REAL, src TEXT, "
          "PRIMARY KEY(entity, ts))")
n = 0
for path in sys.argv[1:]:
    for line in open(path, encoding="utf-8"):
        p = line.strip().split(";")
        if len(p) < 3 or not p[0].startswith("sensor."):
            continue
        ts = int(time.mktime(datetime.datetime.strptime(p[1].strip(), "%Y-%m-%d %H").timetuple()))
        f = lambda i: float(p[i]) if len(p) > i and p[i].strip() not in ("", "None", "-") else None
        c.execute("INSERT OR REPLACE INTO hourly VALUES(?,?,?,?,?,?)", (p[0].strip(), ts, f(2), f(3), f(4), "HA"))
        n += 1
c.commit()
print("importovaných riadkov:", n)
