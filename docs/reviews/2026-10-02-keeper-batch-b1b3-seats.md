# Seat record — KEEPER-BATCH B1 (apply_ops) + B3 (create field fold), HIGH tier, merged from phase/keeper-batch 6b7271c

Writer: plangraph-L2-keeper-batch in-session (anthropic, D-339; foreign writers banned by D-363). 753 passed; max file 396 lines.
Seats on the workstation's local gateway via Taildrop packages (D-319, D-365: no Scaleway, no paid legs), prompt = review-task.txt +
the 3 patches inline (prompt sha256 5f541ebd…0392). Files in $CLAUDE_JOB_DIR/tmp/r6/seat-results/ (job 7caaf7f5).

| seat | model (served == pinned) | call-log / correlation | evidence sha256 | review sha256 | verdict |
|---|---|---|---|---|---|
| A | ovhcloud/gpt-oss-120b (openai) | 1790949035210-968aec / 24096a74-… | ed5d304695285d066ce6c4a220a0d60664749215e60f3a6f3053fe871ebeb4ca | 2efb11f9a7cb7ce6294d71bba1fae1fe3e77a25c38ac11562a41acddce0fbcd8 | FAIL 1H/2M/2L → resolved (D-370) |
| B | ovhcloud/Qwen3.5-397B-A17B (qwen) | 1790949258154-901bd9 / 208cd008-… | 242d91ce8840293fb250a374bc40df19a61f1d5f332ae169dea16f537e8bb7b6 | 1159673b41b2547de7700fd69b2690629fb95a066132e7c690b269e0924cd000 | FAIL 0H/3M/3L → resolved (D-371) |

Rulings (routing-00, D-302 procedure):
- A-F1 High REFUTED: store.py `_write` in batch mode yields with no try, so the exception propagates; store_ops `except BaseException: ROLLBACK`;
  the half-valid-batch test and the ROLLBACK→COMMIT mutation (6 tests fail) prove it.
- A-F2 (unattributable validation message), A-F5 (2xx non-JSON → applied N of N), A-F3/F4 (maintenance): LOW → B2.
- B-F1 stale (1-based at the final head). B-F2 (no pre-lock hash fail-fast; the in-lock re-hash refuses): LOW. B-F3 REFUTED as a defect (the import path
  ends in Store._normalise → _check_text with the cap). B-F5 = the known M2a residual (verifiers not re-run in the txn): B2 adds it to KEEPER.md. B-F4/F6 LOW → B2.
ACCEPTED: D-371.
