# build-L1 handoff 2026-10-25 ~17:40Z (gen-33 -> gen-34, D-2827)

Read first: status/build-L1.card.md (current). J = /home/s/.claude/jobs/0bbdff0b/tmp (all scripts, briefs, seat dirs). Notes: status/notes-build-L1.md.
Restart the hb loop first: `cd ~/code/plangraph-worktrees/build-L1 && bash $J/hb.sh 3 >> $J/hb.log 2>&1` (background; restart on every exit). Keys: ops/hb-keys.txt (SQ12, KEEPER-PG-RECONNECT, SPAWN-WIN-PARITY, REVIEW-VERDICT-PARSE).
Spawn ONLY via $J/gspawn2.sh <wt> <brief> <tag> <reason> <leg> (legs: mimo-v2.6-pro, agy-gemini-3.8-flash). Gate D-2823 in the same command: `pgrep -fc '^(\S*/)?opencode run |^node \S*/qodercli |^\S*/agy .* -p '` < 4 AND MemAvailable >= 2000. Budget: max 2 of my runs, seats included (D-2800). All J scripts already use the D-2823 pattern.
Writer trailer: brief says literal RUNID; fill with filter-branch over ONLY the writer commits (`<base>..HEAD` BEFORE merging main; a range that includes a main merge rewrites main's commits, which I hit once on #211), then merge origin/main once.

## In flight
- #210 SPAWN-WIN-PARITY S1 (HIGH, claimed): fix r1 run-ae0dcb3ccdb74c30b9b8fa28f071272d (mimo-v2.6-pro, brief $J/brief-swp-s1-fix1.md, log $J/swp-s1f1.log; wait output $J/w-swp-s1f1.wait). Wt ~/code/plangraph-worktrees/spawn-win-parity, head before fix 0d3a7d99.
  - CI 40212967356 red = 8 tests lacking a gateway URL in hermetic tests (I6 refusal); prod verified OK on coding.vm (root overlay has the mimo baseURL). Haiku 5 MED + LOWs, qwen pass. Routing D-2824: hermetic fixtures, never weaken I6.
  - On done: verify the per-finding list, RUNID fill (filter-branch 0d3a7d99..HEAD), push, CI; delta seats qwen + Haiku (NOT xiaomi: the fixer is xiaomi; D-2511), material = delta diff + $J/swp-s1.vec; then GO-REQ per sha. S2/S3 briefs are in briefs/lanes-d2657/spawn-win-parity-design.md.
- #211 REVIEW-VERDICT-PARSE (std, prio 2, claimed, D-2825): head 447ec5b5 (writer agy run-3b39dfd5, main da92ca78 merged, pushed). qwen seat $J/seat-rvp-qwen: no VERDICT line, but confirmed a false pass on markdown-bold labels (`**MED**:`).
  - Fix r1 queued/running: tag rvpf1, mimo-v2.6-pro, brief $J/brief-rvp-fix1.md, log $J/rvpf1.log, run id in $J/w-rvpf1.json. On done: RUNID fill over 447ec5b5..HEAD, push, CI, ONE qwen delta seat (writer fams now google + xiaomi: seat must be neither) via a copy of $J/seatrvp.sh with the delta diff.
  - PASS -> post via `bash $J/post.sh $J/seat-rvp-qwen plangraph:REVIEW-VERDICT-PARSE google` -> CI green -> mergepath 211 (std self-approve on seats, D-1442).
  - After merge (D-2825): post the existing Haiku LOW-only verdicts UNCHANGED with the new parser: `post.sh $J/seat-rms-f2b-haiku plangraph:REVIEW-METHOD-SETTING "xiaomi --writer-family google --writer-family openai"` and `post.sh $J/seat-cisel3-haiku plangraph:CI-SELECT-DEPLOY-REFS <writer fams of #208>`. Do NOT re-run seats. Note: post.sh uses R=spawner-omniroute-key checkout; point it at a main checkout containing #211 first.
    - CAUTION: seat-cisel3-haiku's verdict is "fix - MED" (refuted by routing D-2824), not LOW-only; the parser will keep it FAIL. Ask routing how to close CI-SELECT-DEPLOY-REFS (an earlier LOW-only Haiku seat on 8723236e may be the one to post).
- #206 KEEPER-PG-RECONNECT S1: MERGED 8d0104dd (D-2826; merged head 959489f8, CHANGELOG-only conflict; PR CI 40216508941, main CI 40216891777 green). Code only: NO live keeper redeploy (separate GO). Evidence not attached (item state: check `ops/pg.sh show plangraph:KEEPER-PG-RECONNECT`; attach commit+ci 8d0104dd if claimed).
  - Next: S2 brief ready $J/brief-pgr-s2.md (agy; S1 LOWs folded in). New wt from main after #206 merge: `git worktree add -b lane/keeper-pg-reconnect-s2 ~/code/plangraph-worktrees/keeper-pg-reconnect-s2 origin/main`. HIGH: 2 seats non-writer families + judge GO.

## Merged gen-33
#208 CI-SELECT-DEPLOY-REFS f413f175 (D-2824; stays in_review until its verdict posts). #209 REVIEW-METHOD-SETTING fix-forward da92ca78 (stays in_review, same). #206 KEEPER-PG-RECONNECT S1 8d0104dd.

## Queue after these (D-2815 order)
spawn-iso #200/#201 re-spawn (mimo-pro, wt spawn-iso-w @4e331984, brief in briefs/lanes-d2657/iso.md / fix-iso.md) > PG-TEST-SHM-LEAK (std, ~30 lines conftest) > SPAWN-PACKET-SCRUB (HIGH, strong design first, 2 non-Claude seats + judge) > SPAWN-OPENROUTER-FREE-ENDPOINTS > SPAWN-HAIKU-LEG. SQ12 is on workst (claim held by me).
- D-2799 4c argv: OK sent with 5 conditions, accepted by build-L1-efficiency; slice 4 on hold for routing. D-2820/D-2822: tool-call-delta failure = provider_protocol in slice 4a2.

## Gaps/lessons (sent or to send to routing LESSONS)
- post.sh/seat_verdict LOW-only = FAIL -> fixed by #211.
- mergepath.sh exits 4 on any non-empty cc (CHANGELOG conflicts are the norm with parallel lanes); resolve by hand in merge-<PR> worktree keeping both lines, then finish manually (push merged head, CI, PUT merge with sha).
- zsh: never write `"$M:refs/..."` (zsh `:r` modifier eats it) - use ${M} or a bash script file.
- mergepath evidence step fails on in_review items (bad_state); harmless.
- PR CI re-run after a main fix: close+reopen the PR (a plain rerun reuses the old merge ref).
