# Seat record — SYNC-MD-IMPORT v1 (tools/md_sync*.py), merged 841a8c2..807e2d9 (L2 in-session writer, anthropic)
Seats via the workstation (D-319), prompt = review task + 3 patches inline (sha256 54c06fa2…).
| seat | model | corr / call-log | evidence sha256 | review sha256 | verdict |
|---|---|---|---|---|---|
| A | ovhcloud/gpt-oss-120b | eefcd089… / 1790952688157-e7606e | 30d1e5b27e78e38b4ed7ce1fbd9519d9e2b0d2217e3dc8fae2e692444d2a18ae | 714179e1ac35358345ecbe440cbb3a430f06c7b8fe3ba983bc5196b9729ddb23 | PASS (counts on quoted areas; "tests pass" claims unevidenced) |
| B | ovhcloud/Qwen3.5-397B-A17B | 1a373f2d… / 1790953001032-58f00b | effc62b2f26222a18036f2089042a7e96e054d02a12861f9a47fa343bf52bf0f | a04b6acccc3f7c14f37380647dc656c0ea80e69b285f3399742fdc9d63a6212b | FAIL 3H/7M → Highs refuted (H1 md_sync_parse.py:63-66; H2 md_sync_plan.py:39; H3 md_sync.py:79-81), counts (D-302) |
ACCEPTED D-388. Follow-up tests 807e2d9. LIVE 2026-10-02 15:10Z: 44 ops (create 37, edit 1, legacy_state 2, add_evidence 2, close 2), second run 0 ops;
403 items. Timer: ~/.config/systemd/user/plangraph-md-sync.{service,timer,path} + -halt (OnFailure stops timer+path: no retry loop).
