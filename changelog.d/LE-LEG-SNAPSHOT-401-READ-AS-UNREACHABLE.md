Fix leg-state snapshot HTTP 401/403 classification: report `auth_refused` with
the validated gateway URL, HTTP status and caller principal, without keys or
response bodies. Preserve the reason in routing exclusions and run records;
invalidate cached health and avoid transport recovery retries/probes on auth
refusal. Existing Python reader and routing fixes; no credential changes.

Regression: `test_snapshot_auth_refused_gateway[401/403]` replays a local HTTP
gateway and fails against origin/main 25a6693d, then passes with this fix.
Additional coverage checks stale-cache invalidation and saved run diagnostics.
