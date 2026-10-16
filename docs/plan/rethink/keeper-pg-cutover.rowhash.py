"""Per-table content digest sqlite vs PG (read-only): sha256 over rows in PK order.
usage: PG_DSN=... PG_SCHEMA=... python rowhash.py SQLITE_FILE SRC_DIR   (DSN and schema from env only)"""
import hashlib, os, sqlite3, sys, psycopg
sys.path.insert(0, sys.argv[2])
from plangraph.keeper.copy_to_pg import TABLES
s = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
p = psycopg.connect(os.environ["PG_DSN"]); p.execute("select set_config('search_path', %s, false)", (os.environ["PG_SCHEMA"],))
bad = 0
for t in TABLES:   # a rowid-only table would make the digest depend on row order on the PG side
    if not any(r[5] for r in s.execute(f"PRAGMA table_info({t})")):
        sys.exit(f"{t}: no primary key; digest refused")
for t in TABLES:
    info = list(s.execute(f"PRAGMA table_info({t})")); cols = [r[1] for r in info]
    pk = [r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5])]
    q = f"SELECT {','.join(cols)} FROM {t} ORDER BY {','.join(pk)}"
    def dig(rows):
        h = hashlib.sha256()
        for r in rows: h.update(repr(tuple(r)).encode())
        return h.hexdigest()[:16]
    a = dig(s.execute(q)); b = dig(p.execute(q).fetchall())
    ok = a == b; bad += not ok
    print(f"{t:<20} sqlite={a} pg={b} {'ok' if ok else 'DIFF'}")
print("DIFFS", bad); sys.exit(bad > 0)
