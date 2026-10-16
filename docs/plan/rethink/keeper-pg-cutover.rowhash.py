"""Per-table content digest sqlite vs PG (rehearsal only): sha256 over rows in PK order."""
import hashlib, sqlite3, sys, psycopg
sys.path.insert(0, sys.argv[3])
from plangraph.keeper.copy_to_pg import TABLES
s = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
p = psycopg.connect(sys.argv[2]); p.execute("set search_path=rehearsal")
bad = 0
for t in TABLES:
    info = list(s.execute(f"PRAGMA table_info({t})")); cols = [r[1] for r in info]
    pk = [r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5])] or ["rowid"]
    q = f"SELECT {','.join(cols)} FROM {t} ORDER BY {','.join(pk)}"
    def dig(rows):
        h = hashlib.sha256()
        for r in rows: h.update(repr(tuple(r)).encode())
        return h.hexdigest()[:16]
    a = dig(s.execute(q)); b = dig(p.execute(q.replace("ORDER BY rowid","")).fetchall() if pk == ["rowid"] else p.execute(q).fetchall())
    ok = a == b; bad += not ok
    print(f"{t:<20} sqlite={a} pg={b} {'ok' if ok else 'DIFF'}")
print("DIFFS", bad); sys.exit(bad > 0)
