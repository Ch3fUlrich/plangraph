# L2 brief: plangraph:GATEWAY-LOG-TOKEN-MINT (HIGH, D-3350/D-3354, owner build-L1)

Load skill unattended-orchestration (L2 rules: no merge/push to main, no cron, card <= 40 lines, RECYCLE-READY before cap).
Read first: keeper `show plangraph:GATEWAY-LOG-TOKEN-MINT` (desc + done are the contract). Report MERGE-READY <repo> <sha> <pr> per part to build-L1; then stop.

## Rules
- Writers and seats only via the plangraph spawner by card, agy legs first (D-3355: agy-gemini-3.8-flash writer); no Agent subagents, no claude/* legs.
- Seats: 2 per part, family != every writer family of that part (incl. anthropic for any Hand-Edit), no deepseek/kimi (D-3084, D-2511). HIGH: per-vector file:function findings.
- Run the spawn gate in the same command: < 4 live runs and MemAvailable >= 2000 MB.
- Hand-edit cap 30 lines with a Hand-Edit trailer; never AUTH_DOOR_PATHS; writers never write Test-Removal/Gate-Edit.
- Never read, print or create the OmniRoute admin key; never write ~/.omniroute (R-coord-15). Tests use fakes only.
- One writer round carries every seat finding (round cap 3).

## Part A: omniroute-fork (/home/s/code/omniroute-fork, base 6e68fc79; lane from its main/release branch, ask build-L1 which if unclear)
Design (fixed):
1. New scope `logs:read` OUTSIDE the read<write<admin hierarchy: keep ACCESS_SCOPES/SCOPE_RANK as is; add LIMITED_SCOPES = ["logs:read"]; scopeSatisfies("logs:read", any) stays false.
2. Widening trap: src/lib/db/accessTokens.ts:53,148 calls normalizeScope(row.scope) with fallback "read", so a stored "logs:read" would be widened to read today. verifyAccessToken must return the limited scope as is; any other unknown stored scope must yield no access (invalid), never "read".
3. src/server/authz/accessTokenAuth.ts evaluateAccessTokenAuth: scope "logs:read" -> ok only for method GET and pathname exactly /api/usage/call-logs (query string allowed); every other method/path -> insufficient (403). Both gates (managementPolicy and requireManagementAuth) use this function; grep every other reader of `.scope` / normalizeScope (whoami, list, policy) and make each fail closed for logs:read.
4. src/app/api/cli/tokens/route.ts POST: scope enum adds "logs:read"; new expiresInMinutes int 1..1440; reject both expiresInDays and expiresInMinutes at once (400). DB scope column: check migrations/100_cli_access_tokens.sql for a CHECK constraint; add a migration only if needed.
5. Tests (red first): logs:read -> 200 on GET /api/usage/call-logs; 403 on GET /api/keys/<id>/reveal (reveal enabled), GET /api/cli/tokens, another GET management route, POST call-logs; expired (minutes) -> 401; stored unknown scope -> no access; existing scope tests unchanged.
Fork release + gateway redeploy are a LIVE step: build-L1 asks routing GO (D-825); you stop at MERGE-READY.

## Part B: plangraph (lane from origin/main in /home/s/code/plangraph-worktrees/gateway-log-token-mint)
Today: src/plangraph/spawner/gateway_witness.py:20,68-90 reads env PLANGRAPH_GATEWAY_LOG_TOKEN; clients.py keeps it out of the child env.
Design (fixed):
1. New module src/plangraph/spawner/gateway_token.py: mint(job_id, ttl_s) -> (token_id, secret) via POST /api/cli/tokens {name: "plangraph-<job_id>", scope: "logs:read", expiresInMinutes: ceil(ttl_s/60) <= run lease}; revoke(token_id) via DELETE /api/cli/tokens/<id>. Admin bearer read at call time from a 0600 file owned by the current uid (path: env PLANGRAPH_GATEWAY_ADMIN_TOKEN_FILE, default ~/.config/plangraph/gateway-admin.token); wrong mode/owner or missing file -> no mint, witness reason "no gateway admin token file" (seat stays unwitnessed, as today). Secret never logged, never in run record, never in child env, never in an exception text.
2. The runner mints at witness time (in process), passes the secret to witness() as the bearer, revokes in a finally block; a token-id ledger (0600 file, ids + expiry only) lets a sweep revoke leftovers whose run ended or lease expired.
3. Keep the env PLANGRAPH_GATEWAY_LOG_TOKEN path as a fallback until OS-GATEWAY-LOG-ADMIN-KEY is done; minted token wins.
4. Dry-run mode (flag or env) against a fake gateway (local http server in tests): mint, use, revoke proven without any real key.
5. Tests (red first): mint per run with TTL <= lease, revoke at end and on exception, sweep revokes expired ids, secret absent from child env/record/logs/stderr, 0600/owner check refuses 0644 and foreign owner, fake-gateway dry run green.
After both parts merge: build-L1 does live redeploy + keeper/spawner sync (D-825 GO) and the operator places the admin key file (leaf OS-GATEWAY-LOG-ADMIN-KEY).

## Output
Card: /home/s/code/plangraph-worktrees/gateway-log-token-mint/status/card.md (<= 40 lines). Messages to build-L1 only: MERGE-READY / BLOCKED / FAIL, one line each.
