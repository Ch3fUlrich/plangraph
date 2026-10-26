# build-L1 handoff 2026-10-26 ~03:5xZ (gen-36 -> gen-37, routing RECYCLE after 1 compaction)

Read first: status/build-L1.card.md. J = /home/s/.claude/jobs/0bbdff0b/tmp (scripts, briefs, seat dirs). Notes + fix logs: status/notes-build-L1.md. Writer brief tail: status/writer-brief-tail.md (append to every writer/fixer brief).
FIRST:
- hb loop runs detached ($J/hb.sh 90, log $J/hb.log). Restart only if stale >10 min. Keys: ops/hb-keys.txt (SPAWN-ISOLATION-INPLACE is done: drop it; SPAWN-ORPHAN-REAP drop after evidence; PG-TEST-SHM-LEAK added).
- Live state: `PYTHONPATH=~/code/plangraph-worktrees/spawner-main/src python3 -m plangraph.spawner.cli ps`; `gh pr list -R Ch3fUlrich/plangraph`.
- Spawn only via `$J/gspawn2.sh <wt> <brief> <tag> <reason> <leg>` (host gate inside; budget 3). gspawn2 refreshes ~/code/plangraph-worktrees/spawner-main to detached origin/main first (D-2892). glmseat.sh/qwenseat.sh use that same checkout: if you run them alone, first `git -C .../spawner-main fetch -q origin && git -C .../spawner-main checkout -q --detach origin/main`.
- Fill the Writer RUNID by filter-branch over the writer commits only, before pushing. Never force-push (denied); filter only unpushed commits.
- Re-run the brief's test command after every writer/fixer result (D-2889). Writers never write Test-Removal/Gate-Edit/Hand-Edit trailers (D-2885).
- Hand-Edit cap 50 lines per commit (D-2853), never AUTH_DOOR_PATHS; one trailer block (Plan-Item + Hand-Edit + Co-Authored-By, no blank line inside). Haiku cannot seat over Claude Hand-Edits. Gate: `python3 tools/ci_commit_gate.py check --base origin/main --head HEAD`.
- Merges: `gh api -X PUT .../pulls/N/merge -f sha=<full head> -f merge_method=merge -f commit_message="Plan-Item: <KEY>"`, check the result is 40-hex (template $J/finish201.sh: waits head CI, merges, waits main CI, evidence, submit, posts seats). A draft PR gives 405: `gh pr ready N` first. Before merging check `gh pr view N --json mergeable` (CONFLICTING -> merge origin/main into the lane; door paths need a Gate-Edit D-id first, D-2866).

## Done gen-36 (commit+ci evidence attached)
- #219 SPAWN-WIN-PARITY S2 -> 7b388a51 (D-2885). #220 KEEPER-PG-RECONNECT S2 -> f50982d6 (D-2889, code only; live redeploy needs its own GO + OS-KEEPER-LS9-MIGRATE).
- #212 REVIEW-VERDICT-PARSE merged e49b5df8 (item done; now loops for the D-2898 fix log, below).
- #200 SPAWN-ISOLATION-INPLACE -> 2348abf2, item state done.
- #201 SPAWN-ORPHAN-REAP MERGED 2fe388af (r1 mimo-pro, r2 L1 Hand-Edits dc6c2305+05394438, main merge fe54f9cb; qwen delta PASS). finish201.sh was waiting for main CI -> evidence -> submit -> post seat-reap-qwen + seat-reapd2-qwen; check $J/finish201.log, rerun the tail steps if they did not finish.

## In flight (bg jobs at handoff)
1. **REVIEW-VERDICT-PARSE loop, FIRST per D-2900** (glm = only second family for #203, E1a, bus r4, RMS, server lanes). mimo-pro writer ocw1, brief $J/brief-ocw.md, worktree ~/code/plangraph-worktrees/opencode-witness, branch lane/opencode-served-witness, log $J/ocw1.log.
   - (b) spawner: `opencode run --title plangraph-<job_id>`, after exit read opencode.db (XDG_DATA_HOME or ~/.local/share/opencode/opencode.db; tables session_v2.title, session_message type='assistant' data.model{id,providerID}); witness only when it equals the requested model.
   - (a) verdict.py _STANDING_ANSWER_RE `no` branch allows quotes.
   - Next: re-run the brief tests, fill RUNID (run id in $J/w-ocw1.json), gate, push, PR. Seats: families differ from xiaomi (+anthropic only if L1 Hand-Edits) -> qwen + Haiku (Haiku only if no Claude Hand-Edit). spawner/review = HIGH? routing called it a door path -> ask routing whether a judge GO is needed before merge.
   - Then RMS: re-run the glm seat ONCE on the new main (`$J/glmseat.sh <new seat dir with $J/seat-rms-glm2/prompt.txt> rmsglm4`). If served_model is witnessed and the verdict passes: `bash $J/postm.sh <dir> plangraph:REVIEW-METHOD-SETTING "google --writer-family xiaomi --writer-family openai --writer-family anthropic"`. Otherwise report to routing; the item stays in_review.
2. **PG-TEST-SHM-LEAK** PR #222 head e0748276 (agy run-ed01799d, L1 re-run 8 passed, gate 0). qwen seat running (log $J/shmq.log, dir $J/seat-shm-qwen). If it passes: merge (std self-approve), evidence, submit, `bash $J/post.sh $J/seat-shm-qwen plangraph:PG-TEST-SHM-LEAK google`.

## Queue after that
- ORCH-EFFICIENCY-ENFORCE keeper seats: glm, once the witness lands.
- SWP fix log round A (A1-A5 + S2 LOWs, notes) at <=4 findings per round.
- KEEPER-PG-RECONNECT S3 (design WRITER BRIEF S3; HIGH: 2 seats + judge GO).
- SPAWN-PACKET-SCRUB (HIGH prio 3, strong design first).
- SPAWN-OPENROUTER-FREE-ENDPOINTS, SPAWN-HAIKU-LEG.

## Lessons this gen
- An unwitnessed served_model blocks any opencode-leg seat post; qoder seats "witness" = the requested model on its fixed path.
- A seat's TEST-GAMING line with double quotes parsed as missing (fixed by (a)); interim prompt hint "The TEST-GAMING line contains no double quotes." works.
- Merge PRs promptly: #201 went CONFLICTING after #200 landed (runner.py imports, __init__, monitor finally).
