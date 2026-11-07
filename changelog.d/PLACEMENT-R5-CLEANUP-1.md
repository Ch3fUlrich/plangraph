# PLACEMENT-R5 cleanup-1

Carried findings checked against origin/main before editing (paths below are
relative to rust/worker-core/src; evidence refers to the resulting tree).

1. FIXED: synthetic missing batch response, not an invalid revision range:
   finalize_promote.rs:147; finalize_promote_tests.rs:349.
2. FIXED: conflicting-ref assertion accepts only Ref or Cause with Ref kind:
   finalize_promote_tests.rs:138.
3. FIXED: deterministic non-EXDEV link failure leaves no target/ref:
   finalize_promote_tests.rs:376. Concurrent identical import was already covered
   at finalize_promote_tests.rs:181.
4. FIXED: restored defense-in-depth GIT_CONFIG_SYSTEM=/dev/null alongside
   NOSYSTEM; no reason to weaken isolation: finalize_publish.rs:119.
5. FIXED: helper and test share GIT_FIXED_ENV, including expected values:
   finalize_publish.rs:119; finalize_publish_tests.rs:201.
6. ALREADY-FIXED: objects/pack rev-parse retains installation classification
   (with its underlying cause): finalize_promote.rs:227.
7. ALREADY-FIXED: cleanup retries best-effort without masking Objects:
   finalize_promote.rs:167,258; finalize_promote_tests.rs:283.
8. ALREADY-FIXED: git helper takes per-call error kind and retains cause:
   finalize_promote.rs:64; finalize_promote_tests.rs:216.
9. ALREADY-FIXED: resolve uses one batch-check for all objects:
   finalize_promote.rs:132.
10. ALREADY-FIXED: pack trailer checked against content filename:
    finalize_promote.rs:174; finalize_promote_tests.rs:228.
11. ALREADY-FIXED: EXDEV temporary objects mode 0444, forced seam coverage:
    finalize_promote.rs:104; finalize_promote_tests.rs:246.
12. ALREADY-FIXED: fixture pins signing, hooks and templates:
    finalize_promote_tests.rs:11,335.
13. ALREADY-FIXED: explicit GIT_DIR/GIT_SSH_COMMAND absence checked directly
    by exhaustive child environment allowlist: finalize_publish_tests.rs:185,201.
14. ALREADY-FIXED: duplicate explicit-variable absence finding; same exhaustive
    child environment check: finalize_publish_tests.rs:201.
15. ALREADY-FIXED: re-exec asserts exactly one test ran and passed:
    finalize_publish_tests.rs:165.
16. FIXED: D-4788 reachable-history secret scan exists; now one cat-file batch
    process over all reachable objects, with framed binary parsing:
    finalize.rs:173. Trailer validation is separate from secret scanning.

Sweep: promotion/publish error kinds, object modes, batched resolution, and
hermetic Git test settings reviewed. No additional changes needed there.

Validation: 43 finalize tests pass normally and with env -i and a fresh HOME
(installed toolchain binaries on PATH; cached .cargo symlink avoids disk quota).
Clippy all-targets with warnings denied, formatting, and Windows GNU check pass.

Plan-Item: plangraph:PLACEMENT-R5-WRITER-SEATS
