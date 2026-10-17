# L2 brief: build-L2-republish (plangraph:PG-REPUBLISH-FRESH, steps 2-3 prep), parent build-L1
Load skill `unattended-orchestration`. Rules: build-L1 branch docs/plan/rethink/briefs/l2-rules-common.md (binding: D-1704/D-1714 model order, seats with reasons per vector, judge notes, no merge, no live steps, card <= 40 lines, RECYCLE-READY before 230k).
Host: WORKSTATION (tool-using L3 writers via autoos-agent spawn; gitleaks installed there). On the coding VM instead: D-437/D-1554, only tool-less writers/seats.

## Context (operator D-1748, 2026-10-17)
- The public repo gets a fresh one-commit export of main, built with tools/split/make_public.py. The working branches move to the PRIVATE repo Ch3fUlrich/plangraph-work.
- build-L1 did step 1. The private repo holds a verified mirror: 226 refs identical, plus 32 local-only branches under local-codingvm/*.
- Steps 4-5 (force-replace public main, delete public branches, repoint remotes) are build-L1 live steps under a router GO. NEVER push to the public repo's main, never delete branches there, never force-push anywhere.
- Inputs: the private names list and scrub rules live in the private data repo (Ch3fUlrich/plangraph-data, `scrub/words.txt`, `scrub/rules.toml`). Never print or copy their entries anywhere.
- build-L1 dry run at main bd223f11 (`--check`, gitleaks off): REFUSED, 3 private-word hits:
  - src/plangraph/review/gateway.py:21
  - tests/test_opencode_config.py:58
  - tools/seat_verdict.py:30

## Tasks (one closed L3 task each, disjoint files)
1. Fix the 3 hits: config, env or placeholder, with the behaviour unchanged and tests green. One PR to the public main. Standard seats, then MERGE-READY.
2. Manifest gap: tools/split/manifest.toml copies neither .github/workflows, CONTRIBUTING.md, docs/ (except 4 files) nor deploy/. So the export has no CI, while the done criteria require "CI green on the export".
   - Propose a manifest change that copies the workflows + CONTRIBUTING + the docs the public needs. Send the list to build-L1 first, then make it a PR.
   - Every added path must pass the scrub and gitleaks.
3. Export: `make_public.py --check` with the private list and gitleaks, then a real build from main after tasks 1-2.
   - Push the one-commit export ONLY to the private repo, branch `export/main-candidate`.
   - Run the full test suite on the export tree in WSL (pytest -n auto). Do not run it in the private repo's Actions: no GitHub billing (D-1436).
4. Seats on the export: 2 non-Claude seats from distinct families, with reasons per vector:
   - manifest scope;
   - scrub coverage;
   - gitleaks;
   - nothing private in the file list.
   Give each seat the scan commands with their outputs plus the file list, and ask it to verify. The seat evidence stays private (D-1705 format: prompt + served id + verdict + sha256).
5. Draft the step plan for routing (build-L1 sends it):
   - mirror proof (from build-L1);
   - export sha and tree sha;
   - exact force-replace and delete list on the public repo (all refs except main; open PRs; GitHub Pages is not configured);
   - CI impact;
   - rollback = re-push from the mirror.
   - It must answer the dev flow after the cut: where lanes and PRs live, how main stays in sync, where CI runs without billing.

## Report
DONE note: PRs + head shas, export sha, scan counts, test result, seat verdicts, refusals verbatim, open questions.
