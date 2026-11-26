# LE-CI-FLAKE-ENTRY-REPLAY

Outcome. Require replay evidence for changed tests to match the landing part and its applicable environments.

- plan_source. SPEC-CARD-PLAN draft 10, section 4 P1–P3, SHA-256 cae44c2a1179c397a4602789b70aadba05999ac71e396b7b0c4330cbcd100909.
- base commit. 22d34b76a8572b80eb19a4fc6c18327b78d7feda
- card SHA-256. f394a9fd6022638721020b25832ceda04c54a045d50827d801e785530e81255f
- tree SHA-256. 909be45b991d514aba8c9e0aacd3fd087080a794f18ef4bca41e6cd8ef47b3e0
- evidence_after_deploy. Live: On an authorized train, identify the actual readiness consumer and record the true part base, head, selected inventory and five applicable observations. Preserve the ordinary required CI result.

## Decisions

- D1. Rule 3.74 incorporates D-6742 and excludes admission after active train CI starts. D-6743 confers no scope. D-6751 is not reusable for a new workflow change.

## Entry points

- E1. Existing tools/merge_batch_build.py::part_readiness_problems in the legacy train planner; confirm current production consumer before admission.
- E2. Existing tools/ci_select.py::entry_set and ::ready_problems, and the replay receipt generation owned by the admitted planner scope.
- E3. Existing rust/plangraph-cli/src/land/chain.rs::check_push; determine whether the active canonical train calls the predecessor readiness boundary. Outside draft writes.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | A stacked child passes with a valid receipt measured against its own declared parent and refuses an otherwise valid wrong-base receipt. | X1 |
| I1 | E2 | Wrong environment or inventory refuses. Require five outcomes for applicable changed tests including rename and platform applicability. An empty changed-test set uses unchanged ordinary entry gates. | X2 |
| S1 | E2 | A one-in-five inconsistency records FLAKY and retains both streams without overwriting evidence for another environment or replay. | X3 |

## Verification

- X1. E1, changed. pytest tests/test_merge_batch_part_entry.py::test_stacked_readiness_uses_its_parent_receipt. Asserts B1's observable result.
- X2. E2, changed. pytest tests/test_ci_select_entry.py::test_replay_matches_platform_and_changed_inventory. Asserts I1's observable result.
- X3. E2, changed. pytest tests/test_ci_select_entry.py::test_inconsistent_replay_preserves_each_log. Asserts S1's observable result.

Anchors for new or changed fixtures are predicted until checked on the implementation head. Record the actual invoked nodes, results and exact-head receipt.

## Scope and reuse

- R1. Serves B1, I1, S1. Reuse the current entry-set and changed-test selectors, declared parent, hosted-attempt binding and CI status. Keep parent-relative diff stable from refresh through consumption.
- N1. No workflow or gate edits without the exact route grant. No change to active-train or canonical-main merge policy. Pending E3 caller evidence must be resolved before this is an executable contract.

Bind replay evidence for changed tests to the actual landing part before relying on it in READY.

- Scope. Existing `tools/{ci_select.py,merge_batch_build.py,merge_batch_plan.py}`, `tests/{test_ci_select_entry.py,test_merge_batch_part_entry.py,test_merge_batch_build.py}`, `docs/ci-gate.md` and the item's changelog. Reference the historical 999cc9b3 review and c2fc40c3 delta selectively; unrelated hooks, units and interleaved commits are excluded. Adapt fixtures in existing files or use Rust where new implementation or test source is required by rule 9.14.
- Inputs and ownership. For a stacked child the actual comparison base is its declared parent head. For an independent part it is the effective protected base. Derive changed paths, test IDs and environment support from that comparison and current selectors. A receipt belongs to one base, head, environment and exact selected inventory; validate all four even for supplemental environments. Require five outcomes only for applicable changed tests. Implementation-only changes with an empty test inventory retain ordinary entry checks and do not require a replay receipt. A renamed edited test is discovered on the destination path. Environment/attempt log names never collide.
- Tests. Through the real train build readiness consumer, pass a stacked child with a valid own-base receipt and refuse a wrong-base, wrong-inventory or wrong-environment receipt. Exercise one-in-five inconsistency including all log references, stable success, Windows-only selection, rename and empty inventory. Keep the existing slow-hook case green. Check `python3 -m pytest -q tests/test_ci_select_entry.py tests/test_merge_batch_part_entry.py tests/test_merge_batch_build.py` in Linux, then targeted native Windows test-inventory and receipt checks. Record the actual part base and both failed and passing evidence.

## Open points for design approval

- If the implementation must modify the hosted workflow, routing must grant a new exact Gate-Edit before that path is included.
- Confirm which active landing entry point consumes the true-part replay evidence. The legacy builder is a candidate. The Rust chain check establishes ordered ancestry and has no replay receipt input.
