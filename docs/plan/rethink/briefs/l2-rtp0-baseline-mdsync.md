# L2 brief: build-L2-rtp0 — routing:RT-P0-BASELINE + routing:RT-P0-MDSYNC (D-1520, prio 1)
Parent build-L1 (SendMessage `build-L1`). Sonnet, effort medium. Repo routing, cwd /home/s/code/routing-worktrees/build-rtp0,
branch lane/build-rtp0 (from origin/main cdb94e5). Common rules: /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-rules-common.md
(binding; plangraph-specific parts — PR head trailer, plangraph CI, make_public — do not apply: routing has no GitHub CI; trailers use `Plan-Item: routing:<KEY>`).
Telemetry is READ-ONLY. No systemd start/stop/edit of plangraph-md-sync.* or keeper units (live = D-825 GO). No keeper writes. Never push main.

## A. RT-P0-BASELINE (mostly done on main: ad7f072 bin/rt-p0-baseline.py, tests/test_rt_p0_baseline.py, docs/reports/rethink-p0/baseline.{md,json,txt})
Done = script + numbers for 10-08..10-12 AND the current week: Claude weighted tokens/day, burn vs 1/7 weekly, tokens and $ per landed change,
wall time per lane, operator minutes/day; committed, reproducible.
Gaps to close: (1) current-week row was 7 h old -> re-run for "current week" (since 10-15 11:00Z meter reset) to now; keep the 10-08..10-12 row
from the committed baseline.json (do NOT recompute it — gateway retention drift; see baseline.md caveat); (2) "burn vs 1/7 weekly" is only an index
(cap unknown) -> make it explicit: per-day burn / (prev-week observed total / 7), state the assumption; (3) "wall time per lane" — check the
report has a per-lane table (not only median lead); add if missing; (4) Reproduce line must use a repo-relative command (no rtp0 worktree path).
Tests: `python3 -m pytest -q tests/test_rt_p0_baseline.py`. Commit A separately, trailer `Plan-Item: routing:RT-P0-BASELINE`.

## B. RT-P0-MDSYNC (bin/routerctl md_sync_status ~L467, stale_ready ~L495)
Done = md-sync timer+path active, refused=0, a synthetic halt raises a routerctl alert line, green 24 h.
State 18:30Z: timer active (5-min), status file ~/.local/share/plangraph/md-sync.status.json refused=0. md_sync_status already flags
non-active units + refused/error + stale ts. Work:
1. Test (new tests/test_routerctl_mdsync.py): synthetic halt = tmp status file with refused>0 / error / ts > 60 min, and a stubbed
   `systemctl is-active` returning inactive -> each yields its `! md-sync ...` line; healthy fixture -> no line. No real systemd calls in tests.
2. Live proof (read-only): run routerctl's md_sync_status against a COPY of the real status file edited to refused=1 (path arg) -> alert line;
   against the real file -> no refused line. Record commands + output in docs/reports/rethink-p0/mdsync-proof.md.
3. stale_ready: switch from sqlite KEEPER_DB (frozen since PG cutover 16:43Z) to GET http://127.0.0.1:8787/v1/snapshot with
   `Authorization: Bearer <~/.config/plangraph/router.token>` (read; snapshot JSON: items[] with key, state, owner, kind, depends_on, updated_at).
   Same semantics (open, non-gate, updated before cut, all deps done/cancelled). Keeper down/401 -> one `! stale-ready check failed: ...` line.
   Test with a stubbed HTTP response. grep other KEEPER_DB readers in bin/ and tools/ and list them in the report (fix only if trivial, same pattern).
4. 24 h green: record start ts in the proof doc; build-L1 checks at +24 h.
Run `python3 -m pytest -q tests/test_routerctl_*.py tests/test_rt_p0_baseline.py`. Commit B, trailer `Plan-Item: routing:RT-P0-MDSYNC`.

## Writers / seats
Writers per common rules (mimo-v2.6-flash via mimo provider first; D-1504 path only). Standard risk: 1 non-Claude seat PASS on the final diff
(qwen3.8-flash via qodercli, or mimo via ops/seat.sh). Push lane branch (`git push origin lane/build-rtp0`), then MERGE-READY to build-L1:
sha per item, tests output, seat dirs, proof doc path, KEEPER_DB reader list. Then stop.
