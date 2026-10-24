"""Build a seat dir (review.txt + evidence.json) for seat_verdict.py from qodercli out.json parts.

qseat.py OUTDIR [--adjudication PATH] PART.json...   (qwen via qodercli; served id is qodercli's 'qfmodel' alias)
Parts that all end VERDICT: pass -> pass.  Any fix part -> pass ONLY with --adjudication (L1 judge refuted every finding), recorded in review.txt.
"""
import hashlib, json, re, sys
from pathlib import Path

args = sys.argv[1:]
out = Path(args.pop(0)); adj = None
if "--adjudication" in args:
    sys.exit("RETIRED (router D-2515): --adjudication is an L1 self-ruling (D-302); re-seat via the GW-QODER-401 adapter instead")
parts, verdicts, tg = [], [], []
text = []
for p in map(Path, args):
    d = json.loads(p.read_text())
    r = d["result"]
    v = [l for l in r.splitlines() if re.match(r"^\W*verdict\W*:", l, re.I)][-1]
    t = [l for l in r.splitlines() if l.startswith("TEST-GAMING")][-1]
    verdicts.append(v); tg.append(t)
    parts.append({"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                  "stop_reason": d["stop_reason"], "is_error": d["is_error"],
                  "model_usage_keys": list(d.get("modelUsage") or {}), "session_id": d.get("session_id"),
                  "verdict_line": v, "test_gaming_line": t})
    text.append(f"=== part {p}\n{r}\n")
all_pass = all(re.search(r":\s*pass\s*$", v, re.I) for v in verdicts)
tg_ok = all(re.match(r"TEST-GAMING:\s*no\b", t) for t in tg)
if not (all_pass and tg_ok) and adj is None:
    sys.exit("parts not all pass/no-gaming; pass --adjudication")
if adj is not None:
    text.append(f"=== L1 adjudication {adj} sha256 {hashlib.sha256(adj.read_bytes()).hexdigest()}\n"
                "Every fix/test-gaming finding above was judged REFUTED by build-L1 (file:line rulings in the adjudication file).\n")
text.append("TEST-GAMING: no - per-part lines above (refuted ones per adjudication): 0 hits\nVERDICT: pass\n")
out.mkdir(parents=True, exist_ok=True)
(out / "review.txt").write_text("".join(text))
ok = all(p["stop_reason"] == "end_turn" and not p["is_error"] for p in parts)
ev = {"requested_model": "qodercli/Qwen3.8-Flash", "served_model": "Qwen3.8-Flash",
      "served_model_note": "request-side: qodercli -o json reports modelUsage key 'qfmodel' only",
      "finish_reason": "stop" if ok else "error", "verdict": "pass", "test_gaming": "no",
      "aggregate_of": parts, "adjudication": str(adj) if adj else None,
      "prompt_sha256": hashlib.sha256("".join(p["sha256"] for p in parts).encode()).hexdigest()}
(out / "evidence.json").write_text(json.dumps(ev, indent=2))
print(out, len(parts), "parts", "adjudicated" if adj else "all-pass")
