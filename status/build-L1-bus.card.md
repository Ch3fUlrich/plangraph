# build-L1-bus card · gen-2 · 2026-10-25 09:xxZ · NO cron · parent routing (successor pending, router at cap)
Owner build-L1-bus; claims via build-L1.token (sh ops/pg.sh). hb loop: ops/hb.sh 12 over ops/hb-keys.txt, background, restart on exit.
Phase branch phase/rt-p1-bus (origin) head a401b3a9, base main 4608fd3f (main now further). Merge worktree build-L1-bus-phase (detached). Lanes: PR --base phase + gh pr merge --merge --match-head-commit; CI `gh workflow run tests.yml --ref <lane>` (check the push really landed: git ls-remote).
Rules: D-2534/2539/2540/2643 phase batch: 1 review of phase vs main (r1 done), ONE fix round of file-group writers, re-review = 1 seat on delta + touched files, <=3 rounds, then phase PR -> GO-REQ routing. Writers D-2683/2684: agy gemini > mimo-pro > openrouter/free + opencode free > mimo-flash; never Claude. Seats != all writer families (xiaomi, openai, google) -> qwen (+Haiku). D-2694 max_tokens 128000, finish=length empty -> retry at ceiling. D-2706 spawn floor 2000 MB, <=4 writers coding.vm. D-2712 L2 briefs need stop-on-denial rule. Hand-Edit own commit+trailer (D-2240).
Schema fence (build-L1 #164): #164 claims SQLite v14 + PG Migration(8) oversight = collides with bus v14/v15 + PG 8. First to main keeps numbers, second renumbers at its main merge. build-L1 pings on #164 GO.
| Item | State / next |
|---|---|
| FIX r1 G1 keeper | MERGED #186 (c9d4bc38, CI 40168236971 incl PG) |
| FIX r1 G3 exec+wake | MERGED #189 (5feefbf5 + Hand-Edit fmt, CI 40170369570) |
| FIX r1 G2 client | NOT ready: pfg2r2 1f26acb pre-G1 -> 12 fails vs G1-1 text rule; continuation pfg2c on workst (told: merge phase a401b3a9, --json -> text or drop, V6-6/V6-8 per brief, fleet-msg LF + .gitattributes) |
| BUS-REGISTRY-PERSIST (HIGH p8) | MERGED phase #193 (9b2eb4db; writer mimo-pro run ...062933-...a2456f; Hand-Edit PRAGMA test; CI 40174828383). Gap: docs/FLEET-BUS.md paragraph -> delta finding. Claim refused till DISCOVERY done |
| BUS-PEER-DISCOVERY-ADOPT (HIGH p8) | AutoOS MERGED main 2629d740 (D-2730). routing 46b7d6a1 HELD till phase on main + keeper deploy (routing sets env + merges). NO ff live AutoOS checkouts till /v1/hosts deployed. Evidence: l2-cards/discovery-adopt/evidence.md (post after claim allowed) |
| RT-P1-BUS-KEEPER / DISCOVERY / WAKE / EXEC-OBSERVED-STATE / ANY-AGENT-CLI W2a+W2b | IN PHASE, await delta review + phase PR |
| RT-QC-ACCEPTS (HIGH p1) | direct workst writers A-E (B,C,E first; A after G1; D needs Gate-Edit GO-REQ); join via delta |
| routing#3 (864abc10) | HOLD until wake daemon live for routing |
| EXEC-PHASE-LEASE | waits #140 verdict |
| RT-P1-BUS-RUST | blocked (OCGO-BUS-INBOUND, CI-WORKST-RUNNER) |
| BUS-WAKE-STOPGAP-2 / BUS-WATCHER-TRACKED | held; close superseded when BUS-WAKE on main |
NEXT: G2 PHASE-READY -> merge -> delta PROMPT (git diff aded8d82..head + touched files, NO-tools header, IPs redacted) -> qwen seat (qseat.sh) -> fix or phase PR -> GO-REQ. Phase PR body notes: b0648e5 (Writer trailer, not Hand-Edit), bf46b49, 5feefbf5, 9b2eb4db hand edits.
Seat evidence r1: build-L1-bus-seats/phase-r1-aded8d82/ (SHA256SUMS). Haiku verdicts refused by keeper until KEEPER-SEAT-FAMILY-WRITER: keep .out + sha256.
