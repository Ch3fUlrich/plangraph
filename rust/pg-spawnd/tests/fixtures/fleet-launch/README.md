# Fleet launcher parity

Run on Linux from the repository root:

```sh
cargo test --manifest-path rust/Cargo.toml -p pg-spawnd --test fleet_launch_parity
```

The ordinary Rust CI test step runs these fixtures against `pg-spawnd session`
on Linux. The target also finds the
annex revision (`2b5a8c90725e2fdf4d5d5671c3e4589f5e03a5bf`) in a checkout
under `~/code` and stages its launcher and policy to compare both implementations
against the same fixtures. The data worktree is read-only and can be on any
revision. Without the pin, the Rust fixtures run with the included synthetic
policy and settings. To run against a replacement,
set `PLANGRAPH_FLEET_LAUNCH` to
the executable path. It must accept the same CLI and state environment. The test for
editing the Python launcher’s bundled admission policy runs only against the staged
pin, where test settings can be installed inside the disposable directory.
Destination replay uses the installation path in the captured receiver.

The Rust CLI takes the five fleet launcher positionals and the same flags after
`pg-spawnd session`. Set `PLANGRAPH_FLEET_OPS` to use a specific directory for its
policy, default MCP config, style and lean settings. By default it reads
`$ROUTER_HOME/ops`, the installed router’s ops next to its bin directory, or
`./ops` when started in the router directory. The forwarded receiver uses the
same installation path relative to the destination home. Set
`PLANGRAPH_FLEET_REMOTE_ROUTER` if the caller’s ops directory is outside that layout.
These fixtures use temporary state and fake peers for every launch.

For a Rust respawn with a role card, the launcher selects the newest readable
card by its header timestamp, with modification time and path as tie-breakers.
Cards without a valid timestamp use modification time and path and rank below
dated cards. The launcher considers the saved and explicit references,
the recorded session's ready marker, and cards for the role in the applicable
status and referenced directories. It uses the card's labeled drill-down handoff, its
companion file, or the associated ready or launch record path. The successor's
prompt is generated from that one handoff, the positional model and effort,
and the effective caps. Cap precedence is explicit `--ctx-guard`, saved operator
override, then current policy. The caller's original prompt body follows the
generated preamble. Conflicting labeled handoff references, context guard
settings, or explicit compact-window values produce `PROMPT-STALE` with the
supplied and expected values.
Legacy respawns with neither a card reference nor a discoverable card keep
their existing prompt behavior. A referenced card with no usable replacement
causes a refusal.

The harness isolates HOME, state, cwd, and PATH. Rust peers stand in for Claude, SSH,
and Windows memory probes. A destination test delivers the captured envelope to a
second temporary home. Time stamps must be positive integers, and all other fields of
the golden launch and ledger records are compared exactly. The refusal cases check
exit status and both output streams. Dry-run cases also check for unintended writes.

## Annex A, row by row

“Data tests” refers to the existing tests in the pinned data repository’s
`router/tests`. These retain responsibility for caller behavior and implementation
details outside the Rust CLI fixture. A reference to a data test does not mean this
Rust target invokes that test.

| Row | Fixture or reason |
| --- | --- |
| 1 | `refusals::check`, usage synopsis and exit 2. |
| 2 | `dry-run.json`, numbered name, guard and boolean flags. `records::check` covers metadata, parent and saved-session options. |
| 3 | `refusals.json`, bad context guard. |
| 4 | `dry-run.json`, numbered writer. |
| 5 | `refusals.json`, L2 refusal. `check_role_patterns` covers malformed patterns, lookbehind and an unreadable policy. |
| 6 | `records::check_respawn`, selected saved snapshot supplies the cap and metadata. |
| 7 | `refusals.json`, path-like respawn session. |
| 8 | `refusals.json`, unknown and disabled hosts. `remote::check_mirror` pins to the named remote. |
| 9 | `refusals::check`, respawn without a local policy host. |
| 10 | `remote::check`, worker compares two remote hosts by headroom. |
| 11 | `remote::check`, explicit last-resort refusal and reason override. |
| 12 | `remote::check` and `check_selection_errors`, below-floor and empty-admission refusals. |
| 13 | `remote::check_selection_errors`, pool order, unknown memory and missing floor. |
| 14 | `remote::check`, worker headroom and last-resort rule. |
| 15 | `remote::check` and `check_local_memory`, stubbed remote meminfo and real local procfs with an ignored override. `subprocess_capture_times_out_without_waiting_for_exit` checks the timeout path. |
| 16 | `remote::check` and `check_local_memory`, Windows-side reading over SSH and through local WSL interop. |
| 17 | `remote::check_selection_errors`, missing and negative floors. Shipped fleet floors are site policy. |
| 18 | `remote::check` and `refusals.json`, alternate host table, edge flags and local identity. Hostname aliases are site policy. |
| 19 | `legacy_fallback_prefers_the_next_enabled_host_and_can_warn_and_keep_local` checks the fallback helper’s order and exact diagnostics. |
| 20 | `remote::check`, single-line SSH envelope and exact dry-run payload. Destination replay compares the encoded receiver with the pin. `check_mirror` covers the forwarded counter for a real launch. |
| 21 | `remote::check_destination`, captured payload stages prompt/MCP, forwards the number and returns the remote record. Respawn without a spec refuses. |
| 22 | `remote::check_mirror`, preparation error returns uncertainty and never invokes SSH. Transport timeout is covered in data test `test_fleet_launch_hosts.py`. |
| 23 | `remote::check_mirror`, output passthrough, remote record removal from stdout, local host attribution, snapshot and child list. `records::check_respawn` covers the deferred record target. |
| 24 | `remote::check_destination` and `records::check`, child never inherits the forwarding marker. |
| 25 | `dry-run.json` and `records::check_respawn`, default, role and explicit caps. Data test `test_fleet_launch_caps.py` covers invalid policy and cap provenance. |
| 26 | `records::check`, attended window in the guard and launch records. `future_attended_lease_is_deleted_and_window_is_bounded` checks lease validity and cleanup. |
| 27 | `refusals::check_enforcement` and `ledger.json`, required metadata and result shape. Path confinement is a data test in `test_fleet_launch_admission.py`. |
| 28 | `refusals::check_enforcement`, exact real and preview admission messages. Data test `test_fleet_launch_admission.py` covers malformed caps and legacy respawn warnings. |
| 29 | `refusals::check_enforcement`, status 2, refused ledger row and no dry-run writes. |
| 30 | `records::ledger` and `check_private_ledgers`, exact row shape, private file and directory modes, symlinked state root, traversal refusal and FIFO rejection. |
| 31 | `records::check` and `refusals`, started, starting, failed, refused and unavailable ledger. |
| 32 | `refusals::check`, missing cwd. |
| 33 | `refusals::check`, missing prompt, refusal ledger and no child. `records::check` verifies prompt is the last argv element. |
| 34 | `refusals.json`, explicit missing MCP. `dry-run.txt` verifies an absolute path supplied through `FLEET_MCP_CONFIG`. |
| 35 | `dry-run.txt`, pinned style length, and `records::check`, prompt placement. |
| 36 | `refusals::check_io_failures` and `records::check_respawn`, an independently held lock blocks a hand launch before allocation and permits a watchdog-delegated launch. |
| 37 | `records::check_respawn` and `check_id_resolution`, incremental and supplied positive sequence. The interruption suite also contends Python and Rust children on one counter across 9,999. |
| 38 | `remote::check_mirror` and `check_destination`, the caller’s sequence is used on the destination. |
| 39 | `dry-run.txt` and `check_pinned_profile`, generated caps and direct use of an already-pinned lean profile. |
| 40 | `records::check`, persisted handoff/hard settings and both compaction variables. |
| 41 | `refusals::check_io_failures`, obstructed settings directory, exact error and failed ledger row. |
| 42 | `dry-run.txt` plus matrix-specific stderr and no writes. |
| 43 | `dry-run.txt`, complete command, redaction and routing exemption. |
| 44 | `records::check` and `check_respawn`, working directory, role/window, tool PATH ordering and deferred-record env removal. |
| 45 | `records::check` and `refusals::check_io_failures`, reservation shape and refusal before child on reserve failure. |
| 46 | `refusals::check_child_failures`, missing executable, child exit passthrough, and recovery to fallback ledger. |
| 47 | `records::check` and `check_id_resolution`, whole output including final started/unknown line. |
| 48 | `records::check_id_resolution`, full and short IDs, foreign cwd excluded, fresh name fallback, and an unresolved ID after a malformed listing. |
| 49 | `launch-record.json` and `records::check`, complete record and snapshot. |
| 50 | `records::check_respawn`, pending record and new snapshot, published role record untouched. |
| 51 | `records::check` and `check_respawn`, parent normalization, append and deferred suppression. |
| 52 | `records::check` and `check_id_resolution`, OOM file shape, deduplication, no ID warning. |
| 53 | `records::check`, complete guard JSON at the isolated guard path. |
| 54 | `records::check`, a child does not invent a parent record. Data test `test_fleet_launch_admission.py` covers legacy missing specs. |
| 55 | `records::check_respawn`, saved host, refreshed model/effort. |
| 56 | `dry_run`, `remote`, and `records` exercise environment overrides. Data tests `test_fleet_launch_names.py` and `test_fleet_launch_mem.py` cover the independent PATH and FLEET_MEMINFO quirks. |
| 57 | `records::check`, no writes to cwd. The pin supplies style/settings/policy relative to the installed launcher. |
| 58 | Data tests `test_role_relaunch.py` cover fleet-name discovery. It has its own executable and does not start a launcher. |
| 59 | Data test `test_fleet_hosts_probe_retry.py`, discovery retries. Discovery calls `claude agents`, not the launch entry point. |
| 60 | Data test `test_role_relaunch.py`, ready-kick and run-budget behavior. The schedule lives in systemd and is outside the CLI. |
| 61 | Data test `test_role_relaunch.py`, low-memory watchdog gate before launch. |
| 62 | Data test `test_ctx_watchdog.py`, ready markers, live children, and cap gates. Watchdog policy decides whether to call the CLI. |
| 63 | Data test `test_ctx_watchdog.py`, router idle and ready gates. |
| 64 | `refusals::check_io_failures` verifies the launcher side of the shared lock. Data test `test_ctx_watchdog_role_lock.py` covers watchdog contention. |
| 65 | Data test `test_ctx_watchdog_role_lock.py`, watchdog-owned launch stamp and duplicate rejection. |
| 66 | Data test `test_ctx_watchdog.py`, watchdog-owned recycle record and pending evidence. |
| 67 | `dry_run` verifies the launcher’s read-only preview. Data test `test_ctx_watchdog.py` verifies preflight failure prevents stopping. |
| 68 | `records::check_respawn` covers the deferred launch handoff. The pinned watchdog fixture checks that a respawn with a card keeps the original prompt body. |
| 69 | `records::check` verifies the last stdout line the watchdog consumes. Data test `test_ctx_watchdog.py` covers confirmation and stop ordering. |
| 70 | Data test `test_ctx_watchdog.py`, watchdog adopts a younger replacement before any launch. |
| 71 | Data test `test_ctx_watchdog.py`, cleanup after failed/unconfirmed start. |
| 72 | Data test `test_role_relaunch.py`, required roles, throttling, and message forwarding. |
| 73 | Data test `test_role_relaunch.py`, operator stop/start and watchdog recheck. |
| 74 | Data test `test_ctx_watchdog_escalate.py`, alerts sent by watchdog companion. |
| 75 | Data test `test_ctx_watchdog_escalate.py` exercises stuck thresholds and absence across beats. The watch-file shape is owned by `ctx-watchdog-watch` and outside the launcher’s state contract. |
| 76 | Data test `test_ctx_watchdog.py`, first-seen watchdog bookkeeping. |
| 77 | Data test `test_routerctl_sid_refresh.py`, post-launch rewrite. `launch-record.json` includes the job ID used by the reader. |
| 78 | Data test `test_ctx_cap_check.py`, record verifier pass/fail. `launch-record.json` includes launch-time caps. |
| 79 | Data test `test_ctx_cap_check.py`, verification and explicit live flag. The caller owns temporary env setup. |
| 80 | Data test `test_recycle_smoke.py`, preview and enable gate. The pin has no live runner to fixture. |
| 81 | Data tests `test_handoff_check.py` and `test_work_summary.py::test_cards_are_found_through_launch_specs`. The record producers are tested here. Consumers have separate inputs and entry points. |
| 82 | Decision text is operator policy. This suite tests its executable effects, such as L2 refusal, floors and compaction settings. |
| 83 | Skill text and deployment scripts are caller documentation/configuration. The CLI cases exercise the args they pass. |
| 84 | Deployment notes require matching remote installations and shared paths, which are host prerequisites. The simulated destination verifies the prompt and MCP transfer with synthetic files. |

## Companion inventories

Amendment 1 section 3 runs against the pinned Python launcher and Rust in
temporary state. The harness kills each launcher before Claude returns and while
it resolves the ID. It checks exit signals, captured output, ledger rows and
absent launch records. Remote fixtures cover a failed transport, an unavailable
SSH executable and a zero exit with an unresolved ID. The timeout unit test
exercises the same bounded transport at a short deadline. The pinned watchdog
consumes the output of both launchers and exercises the ten-minute rule and
adoption of a younger session. The fixture redirects only its launch subprocess
so the watchdog can keep its existing Python identity import.

The session tools have a distinct MCP interface and on-disk format. These
plangraph tests exercise that contract through the current tool dispatcher or
the injectable Claude subprocess runner.

| Session-tool behavior | Tests under `tests/` |
| --- | --- |
| Dispatch, schema, strict types | `test_spawner_sessions_mcp.py`, `test_spawner_sessions_inputs.py` |
| Name and counter lock, role naming, concurrent writes | `test_spawner_sessions_names.py`, `test_spawner_sessions_r6.py` |
| Private configuration, model/effort/mode/path limits | `test_spawner_sessions_config.py`, `test_spawner_sessions_inputs.py` |
| Cwd and fresh base admission | `test_spawner_sessions.py::test_session_launch_base_admission` |
| Background argv, prompt stdin, fixed settings, env | `test_spawner_sessions_resolve.py::test_argv_linux_is_list_no_shell`, `test_spawner_sessions_plugins.py`, `test_spawner_sessions.py` |
| Duplicate/old-live refusal, read-only preview | `test_spawner_sessions_inputs.py`, `test_spawner_sessions.py::test_dry_run_writes_nothing` |
| Counter reservation, prompt and launch records, ID resolution, audit | `test_spawner_sessions_names.py`, `test_spawner_sessions_audit.py`, `test_spawner_sessions_resolve.py`, `test_spawner_sessions.py::test_record_state_launching_when_run_called` |
| Stop, ready, relaunch, list | `test_spawner_sessions_stop.py`, `test_spawner_sessions_relaunch.py`, `test_spawner_sessions_r8.py` |
| No placement or per-role caps in MCP session launcher | The tool schema has no placement or cap input; configuration’s context cap only scales the list view. |

The writer and reviewer script inventory describes host-local plan-order scripts.
Neither script is in this repository or the pinned data tree. There is no
reproducible script revision available to run with fake harnesses. The following
rows remain documented so the migration can supply script parity fixtures when
those sources are available.

| Script | Inventory rows and related executable coverage |
| --- | --- |
| `writer.sh` | The exact usage/env, exit 9 brief refusal, hook rules, exit 8 disk gate, exit 5 stale base gate, and own-harness message require the script. |
| `writer.sh` | Host admission and leg selection run in the spawner. Exact CLI card/overrides and writer host/leg choices require the script. |
| `writer.sh` | Brief bytes, review delegation, absence of a separate sandbox/quota check, record filenames and final exits 0/3/4 require the script. The current native lifecycle has executable coverage in `rust/pg-spawnd/tests/native_writer.rs`. |
| `review.sh` | The exact usage/env, fast stamp exit 5, shared-clone command and cleanup, prompt concatenation and previous-delta wiring require the script. |
| `review.sh` | Default leg order, family exclusion, remote Codex clone and spawn fallthrough require the script. Family exclusion in the current router has coverage in `tests/test_luna_review_leg.py`. |
| `review.sh` | Exact verdict aliases, weak-verdict exit 6, output names and HARNESS line, disk-cap export and lack of quota guard require the script. |
| Other callers in the inventory | `seat-run.sh`, `agy-review.sh`, and `fleet-writers` are adjacent host scripts. Retired callers and utilities with no launches have no launcher behavior to replay. |
