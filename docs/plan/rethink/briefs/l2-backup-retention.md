# L2 brief: build-L2-backup — plangraph:PG-BACKUP-RETENTION (prio 2, blocks server:RT-P1-PG-DEPLOY; D-1339; parent build-L1)

You are a Sonnet L2 orchestrator. Load skill `unattended-orchestration` first. You brief, spawn and review L3; you never write the
code yourself, never merge to main, never do live steps (no pruning of the real dir). Parent = build-L1 (SendMessage `build-L1`).

## Item — Done means
A retention rule runs from the keeper or a Semaphore playbook (not cron); ~/.local/share/plangraph backups pruned to last N plus
daily for 7 d; keeper.db untouched; du before/after recorded; lands before server:RT-P1-PG-DEPLOY adds disk on the same /.
Context: ~1.6 GB in 153 files, mostly ad-hoc `backup-pre-*.db` (~5 MB each); keeper.db 7.9 MB; / at 81-91%.
Source: Server t6-reports/rtp1-pg-plan.md §1, §7 (server repo; ask server-L1-main for the path if needed).

## Phases
1. Writer (one lane): worktree `/home/s/code/plangraph-worktrees/backup-retention` from origin/main, branch
   `lane/pg-backup-retention`. Pure retention planner (input: file list with mtimes + policy N/daily-days; output: keep/delete
   sets) + `pg admin prune-backups --dir D --keep N --daily-days 7 [--apply]` (dry-run default; prints du before/after and the
   delete list; never touches keeper.db/keeper.json/*.token/WAL/-shm files or anything outside D; only known backup name
   patterns). Hook: the keeper daemon runs the same rule after it writes a backup (config flag, default off), so no cron.
   Hermetic tests (D-852): temp dirs only, no real ~/.local/share path, no live keeper; a guard test that fails if the default dir is
   used without an explicit --dir in tests.
2. One cross-family seat (non-Claude, e.g. ovh/gpt-oss-120b) on the final head; prompt with vector block (wrong file deleted,
   keeper.db/WAL safety, symlinks/path escape, daily-bucket logic/timezone, dry-run default, test-gaming) + append
   /home/s/code/plangraph-worktrees/build-L1/ops/tail.md last. Seat call /home/s/code/plangraph-worktrees/build-L1/ops/seat.sh.
   Adjudicate findings against code; fix round + re-seat if needed. Do not record keeper verdicts; return the seat dir.
3. Read-only dry run against the real dir (`--dir ~/.local/share/plangraph`, no --apply) for the report: counts + du that WOULD be
   freed. The real --apply is a live step build-L1 requests from routing under GO.

## Rules (bind you and every L3; put them in each L3 brief)
- L3 via autoos-agent `route` then `spawn`, free non-Claude first (D-1061); Sonnet only if no free leg (note it). Run finished when
  exit.json exists / `status` says so; never poll job.json. Seats = tool-less gateway calls (max 4 fleet).
- 1 writer at a time. MemAvailable >= 3 GB on coding.vm.
- D-1338: local suite green before every push; never push a red lane; open a draft PR to get CI.
- Commit trailer paragraph last, no blank line: `Plan-Item: plangraph:PG-BACKUP-RETENTION` / `Co-Authored-By: ...`.
- Public hygiene: no hosts, home paths, user names, token-shaped strings in code/tests (default dir via config/XDG, not literal);
  `python3 -m pytest -q tests/test_split_make_public.py` green.
- NEVER touch /home/s/code/plangraph/.claude/worktrees/proposal or the live keeper.
- Denials verbatim + stop that step. Blocked: one line `BLOCKED <item> | <what> | options, *default` to build-L1, keep working.
- Output D-658 caveman. TTL 3 h.

## Report (SendMessage build-L1, <= 30 lines)
head sha, PR number, CI run ids/results, seat dir + verdict + adjudication, dry-run counts/du, open issues, refusals verbatim. Stop.
