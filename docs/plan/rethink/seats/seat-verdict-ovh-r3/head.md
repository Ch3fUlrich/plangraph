You are a code reviewer (cross-family seat) for a small lane of the plangraph keeper repo (Python).

Lane: tools/seat_verdict.py + tests/test_seat_verdict.py. A "review seat" is a tool-less LLM call that leaves a directory with review.txt (the answer, ending in a "VERDICT: pass|fix|halt" line) and evidence.json (served_model, finish_reason, test_gaming, correlation_id, prompt_sha256). The tool parses the verdict, maps the reviewing model to a family, refuses anthropic / the writer's family / unknown families, refuses a PASS unless finish_reason == "stop" and test_gaming != yes, and POSTs /v1/verdict {key, ok, reason} with Bearer reviewer-<family>.token. The keeper currently coerces ok with bool() (excerpt below), so a string "false" would count as a pass: the tool must always send a real JSON boolean. Stdlib only.

REQUIRED ANSWER FORMAT - answer EVERY vector with file:function(:lines) evidence or a concrete finding. A bare "no issues" is rejected.
V1 verdict parsing: can any review text yield ok=True when the reviewer meant fix/halt (template echo "pass | fix | halt", bold/markdown, "PASS with fixes", "not pass", trailing text, multiple VERDICT lines, quoted earlier verdicts)? Can a real pass be missed?
V2 family mapping: misclassification risks (e.g. "o1"/"o3" regex, vendor prefixes, models containing several family words, a claude model named via a router alias). Is refusing anthropic + writer family enforced before any send?
V3 token safety: is the token ever printed, logged, put in argv, or sent anywhere but the given URL? Dry-run redaction?
V4 PASS gates: finish_reason / test_gaming handling, missing fields, non-dict evidence, encoding errors.
V5 tests: hermetic (no real keeper/endpoint reached)? Do they test behaviour or just shapes? Missing cases?
V6 public-repo hygiene: host names, IPs other than 127.0.0.1, absolute home paths, user names, secrets.
V7 anything else.
End with exactly one line: VERDICT: pass | fix (findings by severity HIGH/MED/LOW) | halt.
Round 2: round-1 MED (multi-family names) fixed: ambiguous names now refused; PASS also requires evidence test_gaming == "no". Review the full lane diff below at head 505d5ecd.
FORMAT NOTE: the standing-question answer must be ONE plain line at line start, no bold, no trailing period:
TEST-GAMING: no - searched <where and what>: 0 hits   (or: yes - "<verbatim quote>" / unsure - <what>)
