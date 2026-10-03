# Status card — plangraph-L1-main (L1 project orchestrator; next relaunch: Sonnet high, bypass, D-496/D-506)
Updated: 2026-10-03 11:37 UTC | Brief: routing/docs/plan/briefs/plangraph-L1-main.md | Constraints: CONSTRAINTS.md | Handoff: docs/HANDOFF.md

## Phase
M2a done + pilot keeper live. M2c in progress: UI-BUILD (L2 plangraph-L2-ui-build, keeps running), D-505 keeper/md-sync work, stack note.
Operating-core order APPROVED by routing: MODEL-KINDS (+intent, question/decision/OP kinds, D-row import) -> M2b off-host keeper +
run-bound tokens + MCP remote -> L1s pull from `ready` -> retire md hand-batches.

## Live systems (coding.vm)
- Keeper 127.0.0.1:8787, PID in ~/.local/share/plangraph/daemon.pid (daemon does NOT write it; stop with kill -TERM, never pkill -f).
  Code = worktree head (keeper code as of 610271d + apply_batch rehome + md_sync changes). 489 items. Verify: PG_CONFIG=… pg admin verify.
- md-sync timer + path (~/.config/systemd/user/plangraph-md-sync.*), writes ~/.local/share/plangraph/md-sync.status.json every run;
  halts on refusal -> look at the status file, fix the cause, re-arm. Closes only ANSWERS-cited items today.
- OS-92 (operator): L1 may apply ROUTER-OK'd batches (closes/rehome/priority + plan batches create/edit/priority) under data/updates/.

## In flight (workstation via workst-L1; Taildrop in, read /home/s/fleet/taildrop/plangraph/ only; workst ACKs within 10 min)
- UI T3b-1fix3 DONE but 2 phone fails (A18 collapsed header off-canvas, A19 cross-project Jump lands on stale layout) -> fix4 package
  (L2 pkg/T3b1fix4) at workst, writer run 20261003-064807-t3b1fix4-5a0308 -> probe A18/A19 (+visibleTasks) -> L2 seat prompt (pkg/T3b1seat,
  gpt-oss via review-call + Qwen at workst) -> accept T3b-1 -> T3b-2, T3b-3, T4 (Gemini direct pins, D-510).
- K1 keeper MERGED (D-537, 5b4fe9d..8cdad08), daemon restarted ~11:32Z with per-repo refs; GEN-CLOSEOUT closed. Next: write P1 md-sync (writer run) +
  warning rule "sha ignored on decision-only item <key>"; MEALIE-CAP (D-537) + other new Closes rows (D-527..D-537) go through P1's gated first run.
- P1 md-sync (D-505 + Path 1): router D-rows `Closes: key@sha` -> in_review + evidence -> close when merged (read-only git fetch, fail
  closed); [decision]-only items close by decision; code lanes without @sha -> in_review + listed. OLD pkg mdsync-dclose is HELD at workst.
  FIRST RUN GATE: stop timer+path, dry run, send routing the (key, D-row) list, apply only OK'd keys, re-arm.
- Stack design note: Haiku research run 20261003-065708-sresearch-8424ef running at workst; then write a <=2-page note
  (Python/SQLite vs Rust, Postgres M4, UI libs, phased no-loss migration). Proposal docs/proposals/2026-10-03-graph-tracks-acceptance.md.
- GRAPH 541 items (D-492..D-526 progress batch + server new lanes LIVE, head 00418362643e). GEN-CLOSEOUT closes after K1 (live parser rejects key@sha).
- URL plan.ohje.ooguy.com (read-only, D-525): urlview writer (static_serve.py + render timer, task pkg urlview-pkg) at workst on the AI Studio pin NOW,
  non-Google seats; then I deploy the 2 user units on coding.vm; server-L1 does Caddy/Authelia/DNS (PLAN-VHOST, 100.82.253.38:8089), prox the ACL.

## Rules (full list: docs/process/L2-BRIEF-TEMPLATE.md)
D-437 zero agent runs on coding.vm; D-430/431 spawn only; D-510 writers Gemini 3.8 Flash direct pins, seats other families (never Google
for Gemini code); D-509 plangraph CODE open class, plan DATA private; anti-test-gaming line in every writer task + seat point (real quotes);
D-302 L1 rules on seat findings; record -> ACCEPT -> merge; no push/remote; Taildrop names plangraph__<name>.
## Lessons
Verify a Taildrop file is filed before relaying a path; seats invent quotes for absence claims (check every quote); writers game tests if
the legit test change is not in scope (list it); workst-L1 reads every writer diff for test-gaming; quoted heredocs / Write tool for tasks.
