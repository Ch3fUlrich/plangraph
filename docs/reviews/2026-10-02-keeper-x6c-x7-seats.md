# Seat record — keeper X6c + X7 (HIGH tier, D-228), merged at phase head f17d578

Two counting seats per writer group, of distinct families, both different from the writer. Each seat is gated by
`seat-model-evidence.py` (D-284): exit 0 + all_match, with the sha256 of its evidence file. Evidence files and logs:
`$CLAUDE_JOB_DIR/tmp/r4/`, `tmp/r5/` (job 7caaf7f5). Rulings by routing-00 under D-302 (a fail-with-findings seat counts
once its findings are resolved).

## Writer groups
| group | commits | writer family (model_source) |
|---|---|---|
| G1 | X6c 380683c-equivalent (aec21b0 11df6a3 83a21c9), 92ecd11 (L1 fixture), X7a1 29fc76a, X7a2 881d130, X7b 78635e6, X7c 6d961b4, X7d 9c2988c, FIX-B 909e58e | qoder (client-reported → treated as anthropic, D-237); 92ecd11 claude-opus-5-5 |
| G2 | A' 824b384, C2 785e45a, TOK ac2d348, C3 7d5039f | meta: muse-spark-1.3-contributor-free (the MCP free pin was overridden; spawner bug, reported) |
| G3 | FIX-D 01c86d6, FIX-D2 22bcba4, L2 test split bbb904e, FIX-D3 f17d578 | nvidia: nemotron-3-ultra-free (CLI --free-model, gated request-side: c791c97e…, af546b23…, d0916480…) |
| G4 | d92642e (docs line, L2 Sonnet) | anthropic |

## Seats
| group / slice | run | model | gate sha256 | verdict |
|---|---|---|---|---|
| G1+G4 S1–S5 | 20261002-023742-…-s1-seat1-spark-eba407, 015435-…-s2-…-c8c237, 023742-…-s3-…-139448, 022325-…-s4-…-ceaaad, 031238-…-s5c-…-8b80a9 | spark (meta), gateway | c054f920…, 1c28cfaa…, b41a0700…, ea5db734…, bf26eb9f… | PASS ×5 |
| G1+G2+G4 S3 | 20261002-051609-rev-x7-r4-s3-mimo-6862b2 | mimo (xiaomi) | 5376aebf… | PASS 0/0/5 |
| G1+G2+G4 S5 | 20261002-060733-rev-x7-r4-s5b-mimo-0b95cf (at f17d578) | mimo | ca5d3bd4… | PASS 0/0/7 |
| G2+G3 | in-session Sonnet seat S, 3 passes (83k/54k/38k tokens) | claude-sonnet-5-5 | n/a (in-session) | pass 3 PASS 0/0/1 |
| G3 | 20261002-071310-rev-x7-r5-fixd-spark-a27efe | spark | 73686534… | PASS 0/0/1 |
| G1 S2 | 20261002-082930-rev-x7-r5-s2-nemo-c8048d | nemotron (nvidia) | 33394d2d… | FAIL, refuted (D-302): F1 config-only (config.py:55-58), F2 verify_integrity lifecycle-only by design (events.py:204), F3 in-lock holder check (store_review.py:134-137) |
| G1+G2 S4 | 20261002-085431-rev-x7-r5-s4-nemo-6ad40e | nemotron | a17fe94f… | FAIL, refuted (D-302): F1 none→standard is a raise (store_create.py:233-246), F3 heartbeat lease (events.py:233-242) |
| G1 S1 | 20261002-091633-rev-x7-r5-s1-nemo-1859f3 | nemotron | e076e636… | FAIL, refuted (D-302): M1 priority = definition field folded from hash-chained item_defined events (events.py:95-101); verify_integrity lifecycle-only |

Not counted: gemini-client seats (402/tool loops), vertex (400 / undeclared), deepseek (provider down), mimo tries that rate-limited or
fell through, and nemotron S2 tries a/b (no shell; historical-range misread).

ACCEPTED by routing-00 (D-312).

## Follow-up lanes after the load
FIRST (security, fail-open): a `Closes:` keyword present but no valid key → must fail closed + test (evidence_decisions.py:116-147).

## Open lows (one follow-up lane after the load)
apply_batch usage string omits --allow-no-whoami · batch-path confinement re-check inside the lock · legacy in_review needs evidence first
(intended) · L1 none→standard test (doc test) · apply_batch router-only/resolve_source (by design) · verify_integrity ignores evidence
kind/epoch and `model` is not folded · `X-Waives:` matches the keyword regex · earlier round lows listed in the r3/r4 L2 reports.

## FU-X7-LOWS resolution (2026-10-09, plangraph-L1)
| low | outcome |
|---|---|
| apply_batch usage string omits --allow-no-whoami | already fixed (usage lists it) |
| batch-path confinement re-check inside the lock | already fixed: `close_on_decision` re-reads + re-confines the batch under `_write()` (store_close.py `_read_batch` at the second call) |
| legacy in_review needs evidence first | by design (X7c: the router attaches decision evidence before the close) |
| L1 none→standard doc test | added: tests/test_fu_x7_lows.py (L1 may raise none→standard, may not lower standard→none) |
| apply_batch router-only / resolve_source | by design (batches are router-attributed, D-388) |
| verify_integrity ignores evidence kind/epoch | fixed: per-row (epoch, kind, verified) compared (`evidence_shape`); live DB copy agrees (0 problems) |
| `model` is not folded | by design: `items.model` is unused (0 rows); writer/seat models are the folded PG-MODELS columns |
| `X-Waives:` matches the keyword regex | fixed: Waives:/Closes: must not follow a letter, digit, `-` or `_`; live decisions log: same 119 Closes rows |
| earlier r3/r4 L2 report lows | not recoverable (L2 job dirs gone); the later seat rounds (2026-10-08 since-1006 record) supersede them |
