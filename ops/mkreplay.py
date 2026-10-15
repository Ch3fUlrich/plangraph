import json, sqlite3
T = '/home/s/.claude/jobs/ee78d094/tmp/replay'
s = sqlite3.connect('file:/home/s/.local/share/plangraph/keeper.db?mode=ro', uri=True)
d = sqlite3.connect(T + '/keeper.db'); s.backup(d); d.close()
c = json.load(open('/home/s/.local/share/plangraph/keeper.json'))
c['db'] = T + '/keeper.db'; c['snapshot'] = T + '/plan.snapshot.json'; c['bind'] = '127.0.0.1:18787'
print(sorted(c))
json.dump(c, open(T + '/keeper.json', 'w'), indent=1)
