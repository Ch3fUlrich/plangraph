import sqlite3,sys,json
c=sqlite3.connect(f"file:{sys.argv[1]}?mode=ro",uri=True)
t=[r[0] for r in c.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name")]
print(json.dumps({x:c.execute(f'select count(*) from "{x}"').fetchone()[0] for x in t}))
