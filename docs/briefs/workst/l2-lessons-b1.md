# L2 brief: build-L2-lessons-b1 (OpenCode, cx/gpt-6.1-sol, workst; parent build-L1, D-2265/D-2473)
Load skill unattended-orchestration if your harness has it. Effort medium. Report to build-L1 ONLY (via workst-L1-autoos relay): MERGE-READY <PR> <sha> <seats>, BLOCKED <item> | what | options *default, GO-REQ. No acks/ALIVE. Card <= 40 lines; recycle handoff before 230k.
Repo: AutoOS (spawner tools/autoos-agent*, workst checkout). PUBLIC: never site strings (hosts, IPs, paths under /home), seat prompts or private audit text in commits/PRs/comments.
Keeper claims held by build-L1: do NOT claim/submit/verdict. Own worktree per lane off origin/main; merge origin/main in via merge commit only; NEVER merge another open PR branch into a lane (L9). NO push/merge to main, NO crons, NO live keeper steps.
A permission/classifier denial stops that step: send the verbatim refusal to build-L1.
WRITERS: L3 via autoos-agent spawn only, cx/gpt-6.1-sol; fallback mimo/mimo-v2.6-flash (exact id, never omniroute/mimo; spawner unable to express it = BLOCKED); record skips. Never Claude writers. Each commit: ONE final trailer block `Plan-Item: <key>` + `Writer: <provider/model> <run id>`; Hand-Edit (own commit, <=10 lines, `Hand-Edit: <reason>`). Writer brief = exact FILES list (<=3 code files per writer) + red-first vectors + "when done: report, then stop this session".
SEATS (D-2449/D-2472): 1 non-Claude seat, family != writer (openai): qodercli qwen3.8-flash > opencode free deepseek-v4.1-flash-free / glm-5.3-flash-free (after autoos:SEAT-FREE-OPENCODE merges) > mimo/mimo-v2.6-flash. No deepseek API, no luna over sol code. Seat prompt line 1 exactly: "You have NO tools and no repository access. Everything you need is below. Answer in plain text only." + item text + full diff (tests included). All findings of the seat -> ONE fix writer + same-pattern sweep; re-review the delta only; LOW-only -> no re-seat.
CI: plangraph pytest + pytest-postgres + scrub green on the exact head (gh workflow run tests.yml --ref <branch>); AutoOS its prepush/CI. One heavy suite at a time.

## Lane B1 (plangraph:LESSONS-TO-ALGORITHM part B1; B2 #156 and lane A #158 are done/with L1)
autoos:SEAT-FREE-OPENCODE is merged on AutoOS main 5ce05dd9 (CI green): build on it. Note: opencode glm-5.3-flash-free quota exhausted since ~00:50Z; seats qodercli qwen first, then opencode deepseek-v4.1-flash-free, then agy gemini-3.8-flash.
  B1 AutoOS spawner (one writer per check, FILES named by you after reading tools/autoos-agent*.py and the model registry):
   - L3: spawn refuses a model id not declared in the registry/opencode.jsonc or on a forbidden list (e.g. omniroute/mimo-v2.6-flash, bare mimo-v2.6-flash) with an error naming the allowed id. Overlap: autoos:SEAT-FREE-OPENCODE (declares opencode free ids) — build on it after it merges, do not duplicate.
   - L4: relaunch of a writer whose previous run made no workspace change > 30 min is refused until a gateway quota/429 check for that provider passes (luna shares the codex quota).
   - L5: seat (review role) spawn prepends or requires the NO-tools first line; a seat prompt without it is refused or fixed.
   - L8: seat spawn checks the item's seat clause ("N non-Claude", "non-anthropic") against the seat family; Haiku/Claude refused where the clause excludes it.
Lesson text (copy):
- L1 [code] An operator step is a keeper item (waits_on_operator) + gate edge, never only a card line. Check: card/handoff "IDLE until operator" without an operator item -> flag.
- L3 [code] A model id that differs from the ordered path = STOP; the spawner refuses undeclared/forbidden ids. (omniroute/mimo vs mimo/mimo, 5 failed runs)
- L4 [code] Writer with no workspace change > 30 min: check gateway 429/quota first; never relaunch into the same quota; luna shares the codex quota.
- L5 [code] Seat prompt template, first line "You have NO tools and no repository access..." (qwen run burned without it)
- L8 [code] Read the item's own seat clause before picking seats: "2 non-Claude" excludes Haiku.
- L9 [code] Never merge an OPEN PR into another lane; merge main only.
- L11 [code] Dependency edits on claimed items must be possible for holder/router.
Each check -> its own PR in AutoOS (or one PR per 2 checks if they share a file), red-first test per check, 1 non-Claude seat each (family != writer).
When done (all MERGE-READY or BLOCKED sent): report, then stop this session.
