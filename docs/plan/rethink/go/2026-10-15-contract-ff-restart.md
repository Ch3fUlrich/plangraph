# GO request: RT-P1-CONTRACT ff + keeper ci block + restart (build-L1, 2026-10-15)

Scope: live keeper tree /home/s/code/plangraph/.claude/worktrees/proposal ff d1b0b333 -> 7e5a61e3 (main, PR #5; pure ff, checked),
keeper.json gets a "ci" block, then `systemctl --user restart plangraph-keeper`.

Keeper src delta d1b0b333..7e5a61e3 (rest is docs/tests/rust/tools, inert for the daemon):
- api.py: verdict "ok" must be a JSON bool (string "false" no longer passes); malformed ints -> 400 (was 500).
- store_outbox.py: same bool rule on outbox replay.
Review: ovh/gpt-oss-120b r1 fix -> fixed -> r3 PASS (seats/contract-ovh-r3). CI on 73df7709 + PR green. Suite: 2434 passed.

Config diff (~/.local/share/plangraph/keeper.json, mode 600 kept):
```
-    "snapshot_pubkey": "/home/s/.config/plangraph/snapshot-signing.pub"
+    "snapshot_pubkey": "/home/s/.config/plangraph/snapshot-signing.pub",
+    "ci": {
+        "plangraph": "Ch3fUlrich/plangraph"
+    }
```
(D-1327c. Enables commit+ci evidence for project plangraph via `gh api .../check-runs`; gh at /usr/bin/gh, logged in.)

Steps (never chained with `;`):
1. before: `curl -s 127.0.0.1:8787/v1/health` ; POST /v1/verdict {"key":"plangraph:NO-SUCH-GO-CHECK","ok":"false","reason":"go check"} with a reviewer token (NONEXISTENT key, so the old code cannot record a pass) -> today 404 not_found
2. backup: `cp -p keeper.json keeper.json.bak-20261015-contract` ; sqlite `.backup` of the keeper DB
3. `git -C .../proposal merge --ff-only 7e5a61e3`
4. write keeper.json with the ci block (python json load/dump, chmod 600)
5. `systemctl --user restart plangraph-keeper` && health poll 30 s
6. after: health ok; same POST -> 400 validation (bool check precedes lookup); `pg show plangraph:RT-P1-WORKER-CORE` then add commit+ci evidence fad33f3c -> verified true

Rollback: `git -C .../proposal reset --hard d1b0b333` ; `cp -p keeper.json.bak-20261015-contract keeper.json` ; restart ; health.
If the unit is gone: recreate per live-tree-merge-safety (no reset-failed).
