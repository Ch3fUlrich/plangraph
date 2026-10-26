# build-L1 card · gen-41 END · 2026-10-26 ~12:35Z · NO cron · handoff status/handoff-build-L1.md
J41=/home/s/.claude/jobs/5ce87893/tmp (briefs, seat prompts/results, seat.sh, wsrpm7.sh) · J0=/home/s/.claude/jobs/0bbdff0b/tmp (gspawn2.sh, w-*.wait) · copies ~/.local/share/plangraph/build-L1-seats/gen41
Rules now: D-3035 writers mimo-pro (sol/luna back after one good probe post 14:10Z). D-3072 qodercli PAUSED fleet-wide (seat.sh refuses it). Seats luna > mimo-flash (not where mimo wrote) > Haiku last; glm out (free limit). plangraph spawner only (spawner-main src). No deepseek. Gate <4 runs + Mem>=2000.
| item | state / next |
|---|---|
| KEEPER-FLOOR-LEGACY-CLAIM | PR #246 lane/keeper-floor-waive @30cb14cd (A2 slice1 58465568 + part B 82efb476 + Hand-Edit 30cb14cd); keeper batch 2947 pass. Slice2 doors (API/MCP/CLI) writer run-82eba0b9 running (brief J41/brief-kfw2.md, wait J0/w-kfw2.wait) -> review, commit Writer trailer, push. Seats HIGH: luna (after 14:10Z probe) + kimi opencode-go/kimi-k3.1-pro after #245 merge + probe (D-3074, NO waiver); else next glm reset. Then router judge. qwen run-f7d51f39 VOID (session mix incident). Old lane/keeper-floor-legacy = superseded (part B cherry-picked) |
| OOF G3 + SRI #243 | on hold D-3058; seat1 qwen refutations accepted; seat2 none -> luna after 14:10Z (not over sol code: sol wrote -> luna excluded!) -> kimi/glm; then workst live G3+SRI, G4 bus wake, judge |
| rpm1 ChaptarrNG | r7 DONE: 00709163 fix + 440f2d4b Hand-Edit, qwen PASS (topic OK), image 0.9.977-ng-rpm7 @sha256:6138312c… rev 440f2d4b sent prox-L1-main. Open LOW: 0-byte /tmp/container_rpm_*.db per run |
| workst seat reqs | delivered: mcpsplit PASS, #244 FAIL (+planner fallback answer), AO-ROUTING-SKILL FAIL (verbatim MED5+2 LOW sent). Others dropped (D-3072) |
| leases | OOF, SRI, KFL heartbeat via keeper CLI from build-L1 worktree, keys plangraph:<KEY>; next renew bg at 13:30Z (KFL lease until 13:47Z) |
| D-3023 | delete gen40/gen-seatB, gen-seatB-luna, qoder-backup-20261026-D3022 after gen W merge (guarded, dry run) |
| D-3013 | CI-SELECT + REVIEW-METHOD verdicts after #246 lands |
| D-3062 | OCGO-BUS-SCRUB parked behind floor fix |
| other | MODEL-ROUTE-SPAWN close evidence; D-2918(B) script move; SPAWN cleanup worktree sri-proof (scratch/sri-proof 45bcfd49) |
