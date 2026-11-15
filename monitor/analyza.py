#!/usr/bin/env python3
"""Vyhodnotenie monitoringu TČ zo SQLite - použitie: python3 -I analyza.py [hodiny ...]"""
import sqlite3, sys, time
from collections import defaultdict

DB = "/opt/tc-monitor/tc.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
now = time.time()
E = {"detska": "Sledovaná teplota (Detská izba)", "ciel": "Aktuálna cieľová teplota izby",
     "posun": "Posun ekvitermickej krivky", "wl": "Cieľová ekvitermická voda (WL)",
     "tw2": "Výstupná teplota vody (Tw2)", "vonku": "Vonkajšia teplota", "freq": "Frekvencia kompresora",
     "spotreba": "Denná spotreba TČ", "chyba": "Chybový kód TČ"}


def ser(k, since):
    return c.execute("SELECT ts, value FROM samples WHERE entity=? AND ts>=? ORDER BY ts", (E[k], since)).fetchall()


def rep(label, since):
    print(f"=== {label} (od {time.strftime('%d.%m %H:%M', time.localtime(since))})")
    s = {k: ser(k, since) for k in E}
    d = [v for _, v in s["detska"]]
    if d:
        print(f"detská min {min(d):.1f} max {max(d):.1f} priemer {sum(d)/len(d):.2f} (n={len(d)})")
        cm = dict(s["ciel"]); pairs = [(v, cm[t]) for t, v in s["detska"] if t in cm]
        if pairs:
            print(f"nad cieľom +0,2: {sum(1 for v, g in pairs if v > g + 0.2)/len(pairs)*100:.0f} %, "
                  f"pod cieľom -0,5: {sum(1 for v, g in pairs if v < g - 0.5)/len(pairs)*100:.0f} %")
    p = [v for _, v in s["posun"]]
    if p:
        print(f"posun {min(p)} .. {max(p)}, na -5: {sum(1 for x in p if x <= -4.95)} min, priemer {sum(p)/len(p):.2f}")
    f = s["freq"]
    if f:
        st = sum(1 for a, b in zip(f, f[1:]) if a[1] == 0 and b[1] > 0)
        hrs = (f[-1][0] - f[0][0]) / 3600 or 1
        print(f"kompresor beží {sum(1 for _, v in f if v > 0)/len(f)*100:.0f} %, štarty {st} ({st/hrs:.1f}/h)")
    sp = s["spotreba"]; kwh = 0.0
    for a, b in zip(sp, sp[1:]):
        dl = b[1] - a[1]; kwh += dl if dl >= 0 else b[1]
    print(f"spotreba (odhad) {kwh:.1f} kWh")
    ch = {v for _, v in s["chyba"]}; print("chybové kódy:", ch)
    # potrebný posun vs vonku po hodinách
    H = defaultdict(lambda: defaultdict(list))
    for k in ("vonku", "detska", "posun", "wl", "tw2", "freq", "ciel"):
        for t, v in s[k]:
            H[int(t - t % 3600)][k].append(v)
    print("hod | vonku | izba | cieľ | posun | WL | Tw2 | beh%")
    for h in sorted(H):
        a = lambda k: (sum(H[h][k]) / len(H[h][k])) if H[h][k] else float("nan")
        fr = H[h]["freq"]
        print(time.strftime("%d.%m %H", time.localtime(h)),
              "| %.1f | %.2f | %.1f | %.1f | %.1f | %.1f | %.0f" % (a("vonku"), a("detska"), a("ciel"), a("posun"),
                                                                   a("wl"), a("tw2"), (sum(1 for x in fr if x > 0) / len(fr) * 100) if fr else float("nan")))
    for r in c.execute("SELECT strftime('%d.%m %H:%M',ts,'unixepoch','localtime'), kind, entity, coalesce(text, new) FROM events "
                       "WHERE ts>=? AND kind IN ('tarifa','chyba','system','nastavenie') ORDER BY ts", (since,)):
        print("  ", r)
    reg = c.execute("SELECT count(*) FROM events WHERE ts>=? AND kind='regulacia' AND text LIKE '%posun krivky%'", (since,)).fetchone()[0]
    nan = c.execute("SELECT count(*) FROM events WHERE ts>=? AND text LIKE '%nan%'", (since,)).fetchone()[0]
    print(f"zmien posunu {reg}, výpadkov teploty (nan) {nan}")


for h in (sys.argv[1:] or ["6", "0"]):
    h = float(h)
    rep(f"posledných {h:g} h" if h else "celé obdobie", now - h * 3600 if h else 0)
