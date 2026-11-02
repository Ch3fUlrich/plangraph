# Orchestrator

Role: owns one worktree and branch; spawns and reviews workers.

R-orch-01: Write fixed BRIEF/REPORT fields; one terse line per fact, evidence by pointer; one writer per file. (why: fixed fields parse and stay short; source: common.md Talking to other agents)
R-orch-02: Brief each cheap worker with one file, an exact spec, and file:line anchors. (why: several files invite fabricated completion; source: L1-HANDOFF.md Known traps)
R-orch-04: Feed isolated workers inline or by an absolute read-only path. (why: a clone cannot read outside itself; source: work/L1-routing/Q1doc.out)
R-orch-06: Relaunch, never resume, a no-change child; verify worktree/WIP scope; run the old suite before new tests. (why: wrong tree; unrun WIP adds defects; source: common.md, REDACTFIX2.out)
R-orch-10: Give privileged, installer or state-mutating changes a Sonnet final regardless of cheap verdict. (why: a shallow first pass misses setup defects; source: agysb 8b36913, inbox 23:31:09Z)
