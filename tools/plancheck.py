"""Symbolic pre-check of an L1 plan ops file against plan-v3 + applied batches (until the live keeper is up).
Usage: python3 plancheck.py PLAN.yaml [--base BATCH.yaml ...] [--expect ID,ID,...]"""
import sys, glob, yaml, graphlib, hashlib, argparse, os
ap = argparse.ArgumentParser(); ap.add_argument('plan'); ap.add_argument('--base', action='append', default=[]); ap.add_argument('--expect', default='')
a = ap.parse_args()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
items = {}
for f in sorted(glob.glob(ROOT + '/data/plan-v3/*.yaml')):
    for it in (yaml.safe_load(open(f)) or {}).get('items', []):
        items[f"{it['project']}:{it['id']}"] = dict(it)
def q(p, d): return d if ':' in d else f'{p}:{d}'
for k, it in items.items(): it['depends_on'] = [q(it['project'], d) for d in it.get('depends_on', [])]
probs = []; warns = []
KINDS = {'lane', 'gate', 'goal'}; TIERS = {'standard', 'high', 'none'}; EV = {'commit+ci', 'run-record', 'semaphore-task', 'decision', 'any', 'commit'}
def apply(path, strict):
    b = yaml.safe_load(open(path)); src = b.get('source')
    if src and os.path.exists(src) and hashlib.sha256(open(src, 'rb').read()).hexdigest() != b.get('source_sha256'):
        probs.append(f'{path}: source_sha256 mismatch')
    for n, op in enumerate(b['ops']):
        o = op['op']
        if o == 'create':
          for it in (op['item'] if isinstance(op['item'], list) else [op['item']]):
            it = dict(it); k = f"{it['project']}:{it['id']}"
            if k in items: probs.append(f'op {n}: create {k}: key exists'); continue
            if it.get('kind', 'lane') not in KINDS: probs.append(f'op {n}: {k} kind {it.get("kind")}')
            if it.get('review_tier', 'standard') not in TIERS: probs.append(f'op {n}: {k} tier {it.get("review_tier")}')
            bad = set(it.get('evidence_kinds', [])) - EV
            if bad: probs.append(f'op {n}: {k} evidence {bad}')
            if len(it.get('title', '')) > 200: probs.append(f'op {n}: {k} title > 200 (keeper cap)')
            elif len(it.get('title', '')) > 80: warns.append(f'op {n}: {k} title {len(it["title"])} > 80 (style)')
            it['depends_on'] = [q(it['project'], d) for d in it.get('depends_on', [])]
            it.setdefault('state', 'open'); it.setdefault('kind', 'lane'); items[k] = it
        elif o == 'edit':
            k = op['key']
            if k not in items: probs.append(f'op {n}: edit unknown {k}'); continue
            f = dict(op['fields'])
            if 'depends_on' in f: f['depends_on'] = [q(items[k]['project'], d) for d in f['depends_on']]
            items[k].update(f)
        elif o == 'cancel': items[op['key']]['state'] = 'cancelled'
        elif o == 'close_on_decision': items[op['key']]['state'] = 'done'
        elif o == 'legacy_state': items[op['key']]['state'] = op['state']
for p in a.base: apply(p, False)
before = {k for k, it in items.items()}
nb = len(probs); apply(a.plan, True)
new = [k for k in items if k not in before]
TERM = {'done', 'cancelled'}
for k, it in items.items():
    for d in it['depends_on']:
        if d not in items: probs.append(f'{k}: dangling dep {d}')
        elif items[d].get('state') == 'cancelled' and it.get('state') not in TERM: probs.append(f'{k}: depends on cancelled {d}')
try: list(graphlib.TopologicalSorter({k: [d for d in it['depends_on'] if d in items] for k, it in items.items()}).static_order())
except graphlib.CycleError as e: probs.append(f'cycle: {e.args[1]}')
rev = {}
for k, it in items.items():
    for d in it['depends_on']: rev.setdefault(d, []).append(k)
goals = {k for k, it in items.items() if it.get('kind') == 'goal'}
def reaches(k, seen=None):
    seen = seen or set()
    if k in goals: return True
    seen.add(k); return any(reaches(c, seen) for c in rev.get(k, []) if c not in seen)
for k in new:
    if items[k].get('state') not in TERM and not reaches(k): probs.append(f'{k}: does not reach a goal')
proj = {items[k]['project'] for k in new}
for g in goals & set(new):
    if not any(g in it['depends_on'] for it in items.values()): probs.append(f'{g}: not yet linked into routing:GOAL (router edit needed)')
ready = sorted(k for k in new if items[k].get('kind') == 'lane' and items[k].get('state') == 'open'
               and all(items.get(d, {}).get('state') == 'done' for d in items[k]['depends_on']))
print(f'plan: {a.plan}\nnew items: {len(new)} (kinds: ' + str({kk: sum(1 for k in new if items[k].get("kind") == kk) for kk in KINDS}) + ')')
print(f'problems ({len(probs)}):'); [print('  -', p) for p in probs]
if warns: print(f'warnings ({len(warns)}): ' + '; '.join(warns))
print(f'ready after load ({len(ready)}): ' + ', '.join(r.split(":")[1] for r in ready))
if a.expect:
    exp = {f'{list(proj)[0]}:{e.strip()}' for e in a.expect.split(',') if e.strip()}
    print('expected but not ready:', sorted(exp - set(ready))); print('ready but not expected:', sorted(set(ready) - exp))
