# L2 brief: build-L2-ui-stable (track UI-STABLE, gate PH-UI-STABLE under PH-M2C-UI), parent build-L1
Load skill `unattended-orchestration`. Rules: build-L1 branch docs/plan/rethink/briefs/l2-rules-common.md (binding: D-1704/D-1714 model order and seats, no merge, no live steps, card <= 40 lines, RECYCLE-READY before 230k).
Host: WORKSTATION (tool-using L3 writers via autoos-agent spawn; Playwright needs a real Chromium). On coding.vm instead: D-437/D-1554, only tool-less writers/seats.
Decision: routing D-1730 (operator: fix the static plangraph page now). Spec: plangraph docs/ui/VIEWS.md (V- tags) and the design doc docs/plans/ui/2026-10-17-ui-plan.md (repros R1-R10), both on origin/lane/ui-plan 1b268f9f. Read the spec through git (`git show origin/lane/ui-plan:<path>`); don't merge that lane.

## Items (done criteria = keeper `show plangraph:<ID>`; build-L1 claims, you heartbeat via build-L1)
Waves (write sets must be disjoint inside a wave):
1. UI-VERSION-STAMP (render meta tag + header stamp), UI-BROWSER-TESTS (Playwright suite on a fixture-snapshot page at 1440x900, 1024x768, 390x844; one test per V- tag, xfail with owning item; R1-R10 as tests; runs in GitHub-hosted CI).
2. UI-GRAPH-CLICK, UI-NARROW-LAYOUT, UI-FLAKE-FIXES (each turns its xfail tests green; no test edited to pass).
3. UI-GRAPH-EXPAND, UI-URL-HISTORY.
4. UI-DEAD-CODE (after 2+3).
5. UI-DEPLOY-AUTO, HIGH: repo-side only (render unit, drop-in template, nginx snippet under deploy/, post-deploy stamp check). It must never edit live units, ~/.config/systemd or nginx on the host. The live install is a build-L1 live-sync under a router GO. Today the live render runs from drop-in live-sync-z7-2d61e281 and the keeper-proxy from the frozen proposal tree; never touch either.

## Rules specific here
- CI: GitHub-hosted runners only (public repo, S18/D-1718); no self-hosted, no secrets. Browser suite <= 10 min in CI.
- Tests prove behaviour in a real browser. A V- tag counts as passing only when its test passes un-xfailed.
- Page size: report the size before/after per PR; UI-DEAD-CODE must not grow it.
- One PR per item, Plan-Item trailer, `Writer: <model> <run id>` trailer. Standard items: 2 non-Claude seats from different families than the writer and each other, then MERGE-READY. UI-DEPLOY-AUTO: same + build-L1 sends it to the judge.

## Report
Per item: PR, head sha, CI run ids, seats (model path + verdict), V- tags now passing, page size. Refusals verbatim. DONE note at the end of each wave.
