# Orchestrator

Role: owns one worktree and branch; spawns and reviews workers.

R-orch-01: Write fixed BRIEF/REPORT fields; one terse line per fact, evidence by pointer; one writer per file. (why: fixed fields parse and stay short; source: common.md Talking to other agents)
R-orch-02: Brief each cheap worker with one file, an exact spec, and file:line anchors. (why: several files invite fabricated completion; source: L1-HANDOFF.md Known traps)
R-orch-04: Feed isolated workers inline or by an absolute read-only path. (why: a clone cannot read outside itself; source: work/L1-routing/Q1doc.out)
R-orch-06: Relaunch, never resume, a no-change child; verify worktree/WIP scope; run the old suite before new tests. (why: wrong tree; unrun WIP adds defects; source: common.md, REDACTFIX2.out)
R-orch-10: Give privileged, installer or state-mutating changes a Sonnet final regardless of cheap verdict. (why: a shallow first pass misses setup defects; source: agysb 8b36913, inbox 23:31:09Z)

## L1 rules

R-lead-01: An L1 takes its own named default at once and reports it.
  It waits for the router only for GO rows, waivers and router-only keeper verbs. (why: defaults must not wait; source: D-5642)
R-lead-02: When a route leg fails, the L1 tries the next leg the spawner route offers for that role before sending BLOCKED.
  If the card or order names one model, rule 11 forbids swaps and the L1 backs off or sends BLOCKED. (why: one failed leg is not the end; source: D-5641)
R-lead-03: Every seat card carries its item key. (why: seats need a clear item link; source: D-5658)
R-lead-04: After two design rounds with no agreement, the L1 cuts the item smaller. (why: smaller items help design move on; source: D-5658)
R-lead-05: A fix for a live failure is done only after a live proof run. (why: tests alone do not prove a live fix; source: D-5658)
R-lead-06: Before any statement about seat families the L1 calls the spawner `route` verb. (why: check routing before making claims; source: D-5658)
R-lead-07: No worker, seat or session starts on a base behind the origin default branch.
  Fetch first, cut the worktree at the fetched origin default head, and name the base sha in the card.
  A stale base or a failed fetch stops the spawn. (why: every spawn needs a fresh base; source: D-5661)
