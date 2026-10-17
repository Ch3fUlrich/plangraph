# L2 brief: build-L2-keeper-model — 3 ready plangraph keeper items (HIGH: plan-graph engine / CI verifier; parent build-L1; TTL 6 h)
Rules: /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-rules-common.md (binding, incl. D-1380, PR-head trailer, D-1554 'Writer:' trailer).
HOST workstation (via workst-L1-autoos): tool-using writers allowed there; seats non-Claude: qwen via qodercli, mimo via seat-run7, gemini-3.8-flash via agy (no ovh on workstation).
D-1504 model paths: qwen3.8-flash ONLY via qodercli; mimo-v2.6 via mimo provider/seat-run7; gemini-3.8-flash via agy only (429 = back off, never swap). Sonnet last (record why). D-1272: L2 <= ~10 lines itself.
Items claimed by build-L1 (epoch 1; build-L1 heartbeats). Repo plangraph from origin/main. Never merge, never write to the live keeper (127.0.0.1:8787
read-only), no systemd. Own worktree on the workstation (plangraph-wt-keeper-model). Done text: build-L1 pastes it below; keeper read-only via your usual path.
FIRST audit main against each done text (many parts may exist); report gaps in your first message to build-L1, then close only the gaps.
One branch + draft PR per item, head trailer `Plan-Item: plangraph:<KEY>`, order:
1. MODEL-LOOP: loop kind validated (exactly one exit decision, no orphan); views + validator handle it; tests.
2. MODEL-NAMES: description in schema/API/export; validator WARNING for id-like titles / missing description. The live backfill is a LIVE step: write it as
   a dry-run script + list only, never apply.
3. PG-CI-FORGEJO: Forgejo commit-status adapter (read-only token FILE, never in logs/argv, timeouts/caps, hermetic tests with a fake that matches the real
   API, cite the Forgejo API doc lines); keeper.json ci per project as a PROPOSED config diff in the PR (live config = live step). Token mint is not yours.
New event types or schema change: SQLite + PG parity (schema.py / schema_pg.py migration) and say so in MERGE-READY (live-sync impact).
Seats: 2 FRESH non-Claude families PASS on the final head full diff, per-vector file:function; review.txt + evidence.json (finish_reason=stop,
test_gaming=no) on the workstation, then Taildrop a tar (sha256) to coding-vm per PR. Dismissed fix verdicts are NOT countable: "N countable + M dismissed".
Per item MERGE-READY HIGH <head sha> + PR + CI run + seat dirs to build-L1; continue without waiting. End: one line on live-sync impact per item.
RECYCLE-READY <card> before 230k.

plangraph:MODEL-LOOP | Loop nodes with an explicit exit decision
desc A loop node compresses repeating work (e.g. improve media library, gather user reviews); each loop has an exit DECISION node (D-300.2).
done loop kind validated (exactly one exit decision, no orphan); views and validator handle it; tests
plangraph:MODEL-NAMES | Human-readable titles and descriptions everywhere
desc Every project, phase and task carries a readable title and a description field, not only ids; backfill existing items (D-346.4).
done description field in schema/API/export; every live item has a title of words and a description; check in the validator (warning)
plangraph:PG-CI-FORGEJO | Keeper CI verification per project: Forgejo Actions status adapter (read-only token) + keeper.json ci map
desc D-980. Live keeper.json had no ci section, so commit+ci never verified. GitHub Actions runs exist for plangraph/AutoOS/great-wiki/gen-analysis/Server; Invest CI runs on Forgejo (forgejo.ohje.ooguy.com). Token mint = separate step routing checks. Under a Sonnet L2 when memory allows (D-959).
done Forgejo adapter (commit statuses for the sha, read-only token file via D-944, no token in logs/argv, timeouts/caps, hermetic tests); keeper.json ci per project (GitHub slug where GitHub Actions run, forgejo:<owner>/<repo> where Forgejo runs, none only where truly none); 2 seats incl. non-Claude; judge server-L1-routing; keeper restart under D-915; a commit+ci evidence on invest and on plangraph verifies
