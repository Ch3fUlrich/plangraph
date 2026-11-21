# Fleet launcher parity

Run on Linux from the repository root:

```sh
cargo test --manifest-path rust/Cargo.toml -p pg-spawnd --test fleet_launch_parity
```

The ordinary Rust CI test step discovers this target. The target finds the
annex revision (`2b5a8c90725e2fdf4d5d5671c3e4589f5e03a5bf`) in a checkout
under `~/code` and stages its launcher and policy in a temporary directory.
The data worktree is read-only and can be on any revision. Runners without a
checkout containing the pin print a skip reason. To run against a replacement,
set `PLANGRAPH_FLEET_LAUNCH` to
the executable path. It must accept the same CLI and state environment. The test for
editing the Python launcher’s bundled admission policy runs only against the staged
pin, where test settings can be installed inside the disposable directory.
Destination replay uses the reference checkout’s directory name to match the
Python sender’s installation layout.

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
| 5 | `refusals.json`, L2 refusal. Data test `test_fleet_launch_refused_roles.py` covers malformed policy. |
| 6 | `records::check_respawn`, selected saved snapshot supplies the cap and metadata. |
| 7 | `refusals.json`, path-like respawn session. |
| 8 | `refusals.json`, unknown and disabled hosts. `remote::check_mirror` pins to the named remote. |
| 9 | `refusals::check`, respawn without a local policy host. |
| 10 | `remote::check`, worker compares two remote hosts by headroom. |
| 11 | `remote::check`, explicit last-resort refusal and reason override. |
| 12 | `remote::check` and `check_selection_errors`, below-floor and empty-admission refusals. |
| 13 | `remote::check_selection_errors`, pool order, unknown memory and missing floor. |
| 14 | `remote::check`, worker headroom and last-resort rule. |
| 15 | `remote::check`, stubbed remote meminfo. Data test `test_fleet_launch_mem.py` covers the procfs-only local read and timeout. |
| 16 | `remote::check`, Windows-side reading drives selection and refusal. Data test `test_workstation_slice_a.py` covers local WSL interop. |
| 17 | `remote::check_selection_errors`, missing and negative floors. Shipped fleet floors are site policy. |
| 18 | `remote::check` and `refusals.json`, alternate host table, edge flags and local identity. Hostname aliases are site policy. |
| 19 | Data test `test_fleet_launch_hosts.py` calls the fallback selector directly. Its helper API and fallback diagnostics are outside the launcher CLI. |
| 20 | `remote::check`, single-line SSH envelope and exact dry-run payload. `check_mirror` covers the forwarded counter for a real launch. |
| 21 | `remote::check_destination`, captured payload stages prompt/MCP, forwards the number and returns the remote record. Respawn without a spec refuses. |
| 22 | `remote::check_mirror`, preparation error returns uncertainty and never invokes SSH. Transport timeout is covered in data test `test_fleet_launch_hosts.py`. |
| 23 | `remote::check_mirror`, output passthrough, remote record removal from stdout, local host attribution, snapshot and child list. `records::check_respawn` covers the deferred record target. |
| 24 | `remote::check_destination` and `records::check`, child never inherits the forwarding marker. |
| 25 | `dry-run.json` and `records::check_respawn`, default, role and explicit caps. Data test `test_fleet_launch_caps.py` covers invalid policy and cap provenance. |
| 26 | `records::check`, attended window in the guard and launch records. The lease reader is exercised by data test `test_fleet_launch_caps.py`. |
| 27 | `refusals::check_enforcement` and `ledger.json`, required metadata and result shape. Path confinement is a data test in `test_fleet_launch_admission.py`. |
| 28 | `refusals::check_enforcement`, exact real and preview admission messages. Data test `test_fleet_launch_admission.py` covers malformed caps and legacy respawn warnings. |
| 29 | `refusals::check_enforcement`, status 2, refused ledger row and no dry-run writes. |
| 30 | `records::ledger`, exact row keys, values, numeric timestamp and file mode. Data test `test_fleet_launch_admission.py` covers concurrent and unsafe paths. |
| 31 | `records::check` and `refusals`, started, starting, failed, refused and unavailable ledger. |
| 32 | `refusals::check`, missing cwd. |
| 33 | `refusals::check`, missing prompt, refusal ledger and no child. `records::check` verifies prompt is the last argv element. |
| 34 | `refusals.json`, explicit missing MCP. `dry-run.txt` verifies an absolute path supplied through `FLEET_MCP_CONFIG`. |
| 35 | `dry-run.txt`, pinned style length, and `records::check`, prompt placement. |
| 36 | `refusals::check_io_failures`, an independently held role lock refuses the launch before counter allocation. |
| 37 | `records::check_respawn` and `check_id_resolution`, incremental and supplied positive sequence; `dry-run.txt` uses zero with no write. |
| 38 | `remote::check_mirror` and `check_destination`, the caller’s sequence is used on the destination. |
| 39 | `dry-run.txt`, caps file with the pinned lean profile. Data test `test_fleet_launch_names.py` covers the already-pinned profile branch. |
| 40 | `records::check`, persisted handoff/hard settings and both compaction variables. |
| 41 | `refusals::check_io_failures`, obstructed settings directory, exact error and failed ledger row. |
| 42 | `dry-run.txt` plus matrix-specific stderr and no writes. |
| 43 | `dry-run.txt`, complete command, redaction and routing exemption. |
| 44 | `records::check` and `check_respawn`, working directory, role/window and deferred-record env removed. Data test `test_fleet_launch_names.py` covers PATH additions. |
| 45 | `records::check` and `refusals::check_io_failures`, reservation shape and refusal before child on reserve failure. |
| 46 | `refusals::check_child_failures`, missing executable, child exit passthrough, and recovery to fallback ledger. |
| 47 | `records::check` and `check_id_resolution`, whole output including final started/unknown line. |
| 48 | `records::check_id_resolution`, full and short IDs, foreign cwd excluded, unknown ID. Data test `test_fleet_launch_parent.py` covers elapsed-time and name fallback. |
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
| 68 | `records::check_respawn` covers the deferred launch handoff. Data test `test_ctx_watchdog.py` covers restart prompt generation. |
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
