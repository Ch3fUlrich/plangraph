# D3 prompt transport replan: draft, blocked before implementation

Plan item: `plangraph:SPAWNER-PROMPT-ARGV-LIMIT-REPLAN` (D-5595).
Basis: local `origin/main` 38d6e51db14e394dac6ff43534aee447f12cce0d,
including #775 and #800/S01. This is a proposed design, not an accepted design
or a statement that the tree freeze has been lifted. No launch behavior changes
belong to this note. Re-read all overlapping files against main before writing.

## Evidence and unmet prerequisite

The merged S01 registry and I1 tests establish data compatibility only. Existing
`launch_stdin_payload` still accepts unknown commands as text stdin; the test
`test_legacy_commands_do_not_drop_stdin` explicitly preserves that behavior.
The session adapter constructs Claude `--bg` with an empty positional task and
passes the task through `subprocess.run(input=...)`. Neither observation proves
that the background agent receives it. The registry already forbids `--bg`, but
that restriction is not enforced by S01.

The closed PR #787 body and its
closure comment confirm the design-first replan and final head b8758be7 with
both seats requesting changes. They are not the full findings. The
PR #775 body describes the merged
stdin/file work; its public reviews/comments contain no post-hoc findings.
The required historical findings file named in the D3 work order
is absent from this checkout, all local history for that path, and the searched
local code tree. The full argvfix-a..d and #775 findings therefore cannot yet be
answered. Do not infer their contents from counts, PR summaries or branch diffs.

The package requires another-family acceptance before the writer round. No such
review has been run; this draft is not authority to implement frozen behavior.

## Proposed transport contract

Use the existing versioned registry as the only command-mode authority. Validate
the original command and the resolved executable independently, normalizing
known aliases without allowing a renamed arbitrary executable to claim a mode.
Recognize full command grammar, arity, `--flag=value`, `--`, repeated flags and
required/forbidden modes. Reject unknown modes before constructing a payload.
Validate every repeated metadata occurrence; a later valid value must not hide
an earlier invalid one. Importing launch modules must remain independent of
loading or parsing the resource.

| Launch or helper | Proposed delivery | Required pre-exec evidence |
| --- | --- | --- |
| OpenCode standalone run | UTF-8 stdin, close after complete write | Registry mode, standalone and supported format |
| Claude foreground print | UTF-8 stdin | Foreground print grammar; no background flag |
| AGY print | One user NDJSON record plus EOF | Empty print operand and stream-json input/output |
| Qoder/QoderCLI print | UTF-8 stdin | Empty print operand and text input |
| Codex exec | UTF-8 stdin | `exec` mode and `-` operand |
| Gemini/Qwen print | UTF-8 stdin | Explicit empty print operand |
| Remote overflow helper | Private staged prompt file | Ownership, bounded file read and cleanup; SSH argv contains paths only |
| Rust launch-spec bridge | JSON over stdin | Explicit prompt and stdin carrier agree before storage |
| Claude fleet/background session | Refuse by name in this design | `stdin_prompt_mode_unsupported` before background exec |
| Other prompt-bearing helpers | Private file or proven registered stdin | Otherwise named refusal before exec |

Background refusal is intentional until a separate reviewed protocol proves
actual delivery to the detached agent. Do not silently replace an interactive
fleet launch with foreground print or assume that accepting stdin means delivery.
The session API must persist and return the named refusal with no created agent.

All remaining argv values, including styles, settings, models, titles, wrapper
commands and helper paths, cross a common capacity check immediately before exec.
Measure UTF-8 bytes on POSIX (individual strings plus aggregate argv/environment
and pointer reserve), and the actual quoted UTF-16 command line on Windows;
use the lower batch limit when a shim is involved. Registry capacities are
conservative ceilings, with a bounded fallback if runtime ARG_MAX is unavailable.
Prompt-bearing oversize is `prompt_too_large`, other oversized launch material is
`launch_capacity_exceeded`. Neither can reach exec and fail as Errno 7. Preserve
source attribution: untrusted stdout containing a reason is not a pre-exec refusal.

## Python/Rust boundary and acceptance tests

Share a language-neutral JSON case table derived from the registry. Both Python
`launch_stdin_payload` and Rust launch-spec/prompt storage validation must consume
it. Current main no longer contains the historical `stdin_metadata_carrier`
symbol; inspect `launch.rs` parsing and `runs.rs::store_prompt` instead of patching
a stale function name. Cases cover separate/equal/repeated model/title flags,
UTF-8 boundaries, invalid metadata, missing/wrong carriers, stream framing,
forbidden/unknown modes, argv/environment conflicts, capacity and exact reasons.

For every registered harness, a 300 KiB spawn-to-leg test must execute a fake
harness through the actual service and verify byte count plus digest from the
leg. A direct payload helper assertion is insufficient. The fake runs only from
private temporary roots with Git overrides and credentials cleared; it never
contacts a real host or provider. Cover alias resolution, Windows native/batch
capacity calculations, unknown helper rejection, short prompts, blank prompts,
stream secrets, broken stdin, cancellation and preserved remote file staging.
A background test must assert the named refusal and that exec/agent creation was
never reached. Keep model request-budget admission separate from OS transport.

## Required findings ledger and review sequence

Once the complete findings file is supplied, add one row per original finding:
seat/run, round, severity, exact concern, design paragraph, executable test,
and fixed/refuted decision. Preserve every original identifier, including the
final two MED findings and three HIGH/three MED findings. This draft deliberately
has no invented rows and cannot claim findings closure.

Obtain a non-writer-family design review, record its exact reviewed design hash
and acceptance, then conduct one writer round from updated main. Only after
all rows have evidence should the orchestrator open the implementation PR with
the findings answers, run CI and obtain two different non-writer-family ACCEPT
seats on the complete exact diff. A GO is still required to merge. This worktree
must not push, publish a PR, merge or change the freeze.
