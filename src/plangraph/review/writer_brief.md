Role: implementer. Complete the brief within its scope and report evidence.

Rules:
- Read the named files before editing. Reproduce defects and verify changed behavior.
- Implement general behavior; never skip or weaken tests to obtain a pass.
- Keep callers, docs and changelog consistent. Report unfinished work with its reason.
- Never push. Commit only when the brief authorizes it.
- Use the inherited shared CARGO_TARGET_DIR for all Cargo commands and reviews.
  Never select a private target via environment, --target-dir or Cargo config.
Language: new code is Rust with Rust tests. New Python, shell or Node source files
and allowlist additions require an operator D-id; fixes in existing Python stay allowed.
Gates: cheap checks, changed and affected tests, fast-checks on the exact head, then review.
Commit trailer format (writers only; ALL trailers in ONE final paragraph):
Plan-Item: plangraph:<item>
Writer: <writer identity supplied by the brief>

=== ITEM CONTEXT
Item key: $item_key
Done criteria: $done_criteria
Invariants named by the item: $invariants
Exact base SHA: $base_sha
Exact head SHA: $head_sha
Changed public symbols: $public_symbols
Callers of changed public symbols: $callers
Affected test output (paths): $test_output_paths
