#!/bin/sh
# Denná záloha databáz agenta kúrenia (konzistentná kópia cez sqlite backup), drží posledných 7 dní.
# Po zálohe vyčistí v tc.db staré minútové vzorky (ostane 5-minútový raster), viď RETENTION_FULL_DAYS v config.env.
# KURENIE_DIR (voliteľné) prepíše adresár inštalácie - používa sa na skúšky; predvolene /opt/kurenie.
set -e
K="${KURENIE_DIR:-/opt/kurenie}"
D="$K/backup"
mkdir -p "$D"
T=$(date +%F)
/usr/bin/python3 - "$K" "$D" "$T" <<'EOF'
import sqlite3, sys, gzip, shutil, os
k, d, t = sys.argv[1], sys.argv[2], sys.argv[3]
for name in ("tc.db", "agent.db", "stats.db"):
    src = "%s/%s" % (k, name)
    if not os.path.exists(src):
        continue
    tmp = "%s/%s.%s" % (d, name, t)
    a = sqlite3.connect("file:%s?mode=ro" % src, uri=True)
    b = sqlite3.connect(tmp)
    a.backup(b); b.close(); a.close()
    with open(tmp, "rb") as fi, gzip.open(tmp + ".gz", "wb", 6) as fo:
        shutil.copyfileobj(fi, fo)
    os.remove(tmp)
EOF
find "$D" -name '*.gz' -mtime +7 -delete
cp "$K/config.env" "$D/config.env.bak" && chmod 600 "$D/config.env.bak"

# Čistenie: samples staršie ako RETENTION_FULL_DAYS, ktoré nie sú v 5-min rastri (ts % 300 < 60). events a notes sa nemažú.
/usr/bin/python3 - "$K" <<'EOF'
import os, re, sqlite3, sys, time
k = sys.argv[1]
days = 60
try:
    days = int(os.environ.get("RETENTION_FULL_DAYS") or 0) or None
except ValueError:
    days = None
if days is None:
    days = 60
    try:
        for line in open("%s/config.env" % k, encoding="utf-8"):
            m = re.match(r"\s*RETENTION_FULL_DAYS\s*=\s*['\"]?(\d+)", line)
            if m:
                days = int(m.group(1))
    except OSError:
        pass
days = max(1, days)
db = "%s/tc.db" % k
if not os.path.exists(db):
    sys.exit(0)
limit_ts = int(time.time()) - days * 86400
c = sqlite3.connect(db, timeout=60)
c.execute("PRAGMA busy_timeout=60000")
total, BATCH, MAX = 0, 20000, 200000
while total < MAX:
    cur = c.execute("DELETE FROM samples WHERE rowid IN (SELECT rowid FROM samples WHERE ts<? AND ts%300>=60 LIMIT ?)", (limit_ts, BATCH))
    c.commit()
    total += cur.rowcount
    if cur.rowcount < BATCH:
        break
    time.sleep(0.2)
c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
c.close()
print("Čistenie databázy: zmazaných %d riadkov samples starších ako %d dní." % (total, days))
EOF