"""Deterministic SYNTHETIC snapshot for UI work (no real plan data).

Usage: python tests/fixtures/ui/make_synthetic.py > tests/fixtures/ui/synthetic_snapshot.json
Adds PROPOSED extension fields the real snapshot does not yet carry: top-level
`questions` / `decisions`, item `loop` ({max, exit_decision}) and `phase` mirrors `track`.
"""
import json
import random

R = random.Random(42)
PROJECTS = {"atlas": "T", "borealis": "B", "cinder": "C", "dune": "D"}
PHASES = ["P0 Foundations", "P1 Core build", "P2 Integration", "P3 Hardening", "P4 Launch"]
STATES = ["open", "claimed", "in_review", "done", "cancelled"]
OWNERS = ["Atlas L1-main", "Borealis L2", "OPERATOR", "Cinder L1-main", "Dune L2", "router"]
WORDS = "ingest parse index shard route cache verify sign publish rotate audit replay plan merge lease queue probe gate fence".split()
LONG = ("Reconcile the distributed lease ledger against the replicated audit trail and "
        "emit a compact signed attestation for every closed epoch")
EVID = ["commit+ci", "run-record", "semaphore-task", "decision"]
NOW = "2026-10-01T00:00:00Z"


def title(i):
    if i % 37 == 0:
        return LONG
    return f"{R.choice(WORDS).capitalize()} {R.choice(WORDS)} {R.choice(WORDS)} step {i}"


items = []
for pi, (proj, pre) in enumerate(PROJECTS.items()):
    n = 62 if pi < 2 else 63
    keys = []
    goal = f"{proj}:GOAL"
    for i in range(n):
        ph = PHASES[min(i * len(PHASES) // n, 4)]
        iid = f"{pre}-{i + 1}"
        key = f"{proj}:{iid}"
        pool = keys[-8:]
        deps = R.sample(pool, min(len(pool), R.choice([0, 1, 1, 2, 3]))) if pool else []
        if keys and R.random() < 0.06:  # cross-project dep
            other = R.choice([k for k in PROJECTS if k != proj])
            deps.append(f"{other}:{PROJECTS[other]}-1")
        # states trend: early phases done
        frac = i / n
        st = "done" if frac < 0.35 and R.random() < 0.9 else R.choice(STATES[:3] + ["open", "open", "cancelled"] if frac > .6 else STATES[:3])
        if frac > 0.85:
            st = "open"
        kind = "gate" if i % 12 == 11 else "lane"
        it = {"project": proj, "id": iid, "key": key, "title": title(i), "kind": kind, "track": ph,
              "phase": ph, "owner": "OPERATOR" if i % 17 == 5 else R.choice(OWNERS),
              "state": st, "estimate_h": R.choice([1, 2, 3, 4, 6, 8]), "depends_on": deps, "corrects": None,
              "done_criteria": "Evidence exists and a reviewer confirmed it. " * R.choice([1, 2, 4]),
              "evidence_kinds": [R.choice(EVID)], "operator": i % 17 == 5, "priority": R.choice([0, 0, 0, 1, 2]),
              "epoch": 1, "evidence": [], "updated_at": NOW, "claimed_by": None, "lease_until": None,
              "worker_run": None, "reviewer_run": None, "review_tier": R.choice(["standard", "high"])}
        if st == "done":
            it["evidence"] = [{"kind": it["evidence_kinds"][0], "ref": f"{R.randrange(16**7):07x}", "verified": True, "at": NOW}]
        if i % 23 == 7 and kind == "lane":  # loop node with an exit decision
            it["loop"] = {"max": 5, "iteration": R.randrange(1, 4), "exit_decision": f"D-{100 + i}"}
            it["title"] = "Repeat review cycle until exit decision " + it["loop"]["exit_decision"]
        items.append(it)
        keys.append(key)
    last = [k for k in keys[-6:]]
    items.append({"project": proj, "id": "GOAL", "key": goal, "title": f"{proj.capitalize()} goal reached",
                  "kind": "goal", "track": PHASES[-1], "phase": PHASES[-1], "owner": "router", "state": "open",
                  "estimate_h": 0, "depends_on": last, "corrects": None, "done_criteria": "all lanes done",
                  "evidence_kinds": ["decision"], "operator": False, "priority": 0, "epoch": 1, "evidence": [],
                  "updated_at": NOW, "claimed_by": None, "lease_until": None, "worker_run": None,
                  "reviewer_run": None, "review_tier": "standard"})

questions = [{"id": f"Q-{n}", "project": R.choice(list(PROJECTS)), "text": f"Should we pick option {n} for the {R.choice(WORDS)} stage? " + "Context sentence. " * R.choice([1, 3]),
              "asked_by": "Atlas L1-main", "status": R.choice(["open", "open", "answered"]), "item": R.choice(items)["key"],
              "answer": None} for n in range(1, 13)]
for q in questions:
    if q["status"] == "answered":
        q["answer"] = "Yes, go with the simpler option."
decisions = [{"id": f"D-{n}", "project": R.choice(list(PROJECTS)), "title": f"Decision {n}: {R.choice(WORDS)} policy",
              "text": "Adopt the stated policy for all new work.", "date": "2026-09-%02d" % (1 + n % 28), "items": [R.choice(items)["key"]]}
             for n in range(100, 118)]
json.dump({"generated": NOW, "head": "synth01", "items": items, "questions": questions, "decisions": decisions},
          __import__("sys").stdout, indent=1)
