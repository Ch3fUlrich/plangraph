# LE-DELL-TMP-QUOTA-FAILS-RELEASE-INSTALL-TESTS

Outcome. host-ready reports tmp_low when the TMPDIR of a host's launch environment is on tmpfs or has less free space than the floor, and a release install that fails on a disk quota or full disk says it is a host fault.

- plan_source. Keeper item plangraph:LE-DELL-TMP-QUOTA-FAILS-RELEASE-INSTALL-TESTS, 2026-11-27 16:16Z, priority 10.
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- card SHA-256. deffec49292951eec5ed69bb6b7d01586d42aa4aee33ef4890d5650f96996830
- tree SHA-256. ccbb630a6f1df9eb35b9d5ff149e1b2a2acad4b7f582fda4dddb028337392aa0
- evidence_after_deploy. Host step, not code (item How (1)): the dell host session sets `TMPDIR=/mnt/data/plangraph/tmp` (directory mode 0700) in `~/.config/environment.d/`, and in `feeder.env`, sets `tmp_floor_gb` in its `hosts.json` entry only if the default does not fit, then restarts the user units. Then: `python3 -m plangraph.spawner.host_ready --host dell --json` shows `tmp_dir` under `/mnt/data` and no `tmp:` failure, and one full fast-check run of a doc-only lane on dell passes `plangraph-cli/tests/host_sync_release.rs`. Record both in the item evidence.

## Checked against base

- host-ready measures memory, repo disk and work mounts, never the temporary directory: no `TMPDIR` read in the probe. [src/plangraph/spawner/host_ready_source.py:270-390]
- The probe runs remote steps through `command(check, script, steps, **plan)`; the `spawner()` helper shows how a step plan carries the host `env_file`, which is the spawner launch environment. [src/plangraph/spawner/host_ready_source.py:283-309,351-367]
- Every failure becomes one stable line `HOST-NOT-READY <host> <check> <reason>`. [src/plangraph/spawner/host_ready.py:785-790; docs/placement-rules.md:326-328]
- A release install error is printed as `FAIL release install: {error}`; the dell failure was `Disk quota exceeded (os error 122)` under a tmpfs `/tmp` with 9 GB free, so free bytes alone do not show a per-user tmpfs quota. [rust/plangraph-cli/src/host_sync_release.rs:235-255]
- `.docs-map` maps `src/plangraph/spawner/**` to `docs/SPAWNER.md`, which has no line headroom (668 lines against its 728 cap less 60); a new `docs/spawner/<ITEM>.md` fragment satisfies the docs gate instead. [.docs-map; tools/check_file_size.py:47-52; tools/check_changelog_docs.py:393-400]
- The registry projection does not carry `env_file` or any floor into the overflow host entries, and the synthesized local entry in `host_ready.main` copies a fixed field list; a new per-host field must be added to both. [src/plangraph/fleet_sync/genconfig.py:74-84; src/plangraph/spawner/host_ready.py:749-763]
- On the work hosts the feeder and the spawner session are systemd user units, so their TMPDIR is the user manager's environment (environment.d) plus a unit's own EnvironmentFile; an ssh session's TMPDIR is not theirs. [deploy/systemd/pg-feeder.service]
- The release install wraps the lock error with `io::Error::other` (the errno is lost) and runs `cargo build` with inherited stderr, so a quota failure inside Cargo reaches the caller only as `release build: exit status: 101`. [rust/plangraph-cli/src/host_sync_release.rs:144-183; rust/worker-core/src/lockfile.rs:22-27]
- The shared test runners in `tests/test_host_ready.py` and the ssh stub in `tests/test_host_ready_path.py` answer unknown scripts with empty output, so a new probe needs one branch in each or every existing green test reports `invalid_response`. [tests/test_host_ready.py:30-51; tests/test_host_ready_path.py:20-36]

## Decisions

- D1. New host-ready check `tmp`, run for every reachable host right after the repo `disk` measure. The directory measured is the one the host's launched processes get: when `config["env_file"]` is set (a hand-written overflow entry), the step plan carries `env_file` exactly as `spawner()` does and the directory is `${TMPDIR:-/tmp}` after sourcing it; otherwise it is the `TMPDIR` of the user systemd manager (`systemctl --user show-environment`), falling back to the session's `${TMPDIR:-/tmp}`. Commands (no `df`, so existing `df -Pk` test branches do not match): with env_file `sh -c 'd="${TMPDIR:-/tmp}"; printf "%s\n" "$d"; stat -f -c "%T %a %S" -- "$d"'`; without it `sh -c 'm=$(systemctl --user show-environment 2>/dev/null | sed -n "s/^TMPDIR=//p"); d="${m:-${TMPDIR:-/tmp}}"; printf "%s\n" "$d"; stat -f -c "%T %a %S" -- "$d"'`.
- D1a. `tmp_floor_gb` is a per-host registry field: `genconfig.generate` copies `host["tmp_floor_gb"]` into the overflow entry when present, and `host_ready.main` copies `local.get("tmp_floor_gb")` into the synthesized local entry when present. `probe_host` accepts an int or float with `0 < value <= 1024` and fails `tmp: invalid tmp_floor_gb` otherwise (no measurement verdict then).
- D2. Parse: line 1 `tmp_dir`, line 2 `<fstype> <available blocks> <block size>`; `tmp_free_gb = blocks * size // 1024**3`. The record gains `tmp_dir`, `tmp_fs`, `tmp_free_gb` (all `None` when unmeasured). A parse error is `tmp: invalid_response`.
- D3. Failures (check `tmp`, reason starts with `tmp_low`): `tmp_low: <dir> on tmpfs` when `tmp_fs == "tmpfs"` (a tmpfs quota is not visible in free bytes; the dell incident had 9 GB free); otherwise `tmp_low: <dir> free <n> GB, floor <f> GB` when `tmp_free_gb < config.get("tmp_floor_gb", 4)`.
- D4. EXPERIMENT (rule 3.76) for the 4 GB default: the release install writes the host binaries of one train (a few hundred MB) plus test temp trees; 4 GB is roughly ten times that. Measure `tmp_free_gb` minima across hosts in host-ready records for 7 days and set `tmp_floor_gb` per host in `hosts.json` when needed.
- D5. Release install message. Errno is kept on every path that can hit the disk: the lock maps `LockError::Io(e)` to `e` unchanged (`Timeout` stays a plain error); `cargo build` runs with stderr piped through a thread that copies each line to our stderr and remembers whether any line contains `Disk quota exceeded`, `No space left on device`, `(os error 122)` or `(os error 28)`; a failed build with such a line returns `io::Error::new(io::ErrorKind::StorageFull, "release build: <status>: <first such line>")`. `run` maps an error whose `raw_os_error()` is 122 (EDQUOT) or 28 (ENOSPC), or whose kind is `StorageFull`, to `FAIL release install: host fault: no room to write (TMPDIR=<TMPDIR or /tmp>): {error}`; other errors keep today's text. The helper `fn failure_line(error: &io::Error, tmpdir: Option<&OsStr>) -> String` holds the mapping so it is unit tested without filling a disk. The integration test prints the CLI output on failure, so its message names the host fault without a test change.

## Entry points

- E1. `python3 -m plangraph.spawner.host_ready` (`probe_host` in `host_ready_source.py`).
- E2. `host-sync --install-release` (`host_sync_release::run`).

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | TMPDIR on tmpfs: record `tmp_fs == "tmpfs"`, failure `tmp: tmp_low: /tmp on tmpfs`, `ok` false, stderr/stdout line `HOST-NOT-READY fiction tmp tmp_low: /tmp on tmpfs`. | X1 |
| B2 | E1 | ext4 TMPDIR with 2 GB free and default floor: `tmp: tmp_low: <dir> free 2 GB, floor 4 GB`; with `tmp_floor_gb: 1` the host is ready. | X1 |
| B3 | E1 | With `env_file` configured the step plan carries `env_file`; without it the command reads the user manager environment; a green host reports `tmp_dir`, `tmp_fs`, `tmp_free_gb` and no failure. | X2 |
| B3a | E1 | Through `host_ready.main` with a registry fixture, a host whose registry entry has `tmp_floor_gb: 1` is ready at 2 GB free and one with `tmp_floor_gb: "x"` fails `tmp: invalid tmp_floor_gb`; `genconfig.generate` carries the field into `overflow-hosts.json`. | X2 |
| B4 | E2 | `failure_line` for os error 122 and 28 and for kind `StorageFull` starts `host fault: no room to write (TMPDIR=`; for os error 2 it is the plain error text. `host-sync --install-release` with a fake `cargo` whose build prints `error: failed to write: Disk quota exceeded (os error 122)` and exits 101 prints `FAIL release install: host fault: no room to write` and exits 1. | X3 |

## Verification

Write X1-X3 first; on the base X1/X2 see no `tmp` key or failure and X3 does not compile.

- X1. `tests/test_host_ready_tmp.py::test_tmpdir_on_tmpfs_or_below_the_floor_is_tmp_low` (parametrized; reuse `CONFIG` and `runner` from `test_host_ready`, answer the `stat -f` script per case).
- X2. `tests/test_host_ready_tmp.py::test_tmp_probe_runs_in_the_launch_environment` (record the argv/body the fake runner receives and assert `env_file` is part of the plan) `::test_green_host_reports_tmp_fields`, `::test_floor_is_carried_from_the_registry_and_validated` (registry fixture through `host_ready.main` as `tests/test_host_ready_path.py` runs it, plus `genconfig.generate`).
- X3. `rust/plangraph-cli/src/host_sync_release.rs::tests::quota_and_full_disk_are_named_host_faults` and `rust/plangraph-cli/tests/host_sync_release.rs::a_quota_failure_inside_cargo_is_a_named_host_fault` (the existing fake-`cargo` fixture, answering `metadata` and failing the build).
- X4. Held out: `tests/test_spawner_doctor.py` and `tests/test_fleet_sync.py` stay green; `tests/test_host_ready.py` and `tests/test_host_ready_path.py` pass with only their new healthy branch (`elif 'stat -f' in script: output = '/tmp\next2/ext3 2250000 4096\n'` and `*"stat -f"*) printf '/tmp\next2/ext3 2250000 4096\n';;`).

## Scope and reuse

- R1. Reuse `measure`, the step-plan transport with `env_file`, `fail`, the HOST-NOT-READY line format and the registry projection; no new transport, no sudo, no quota tool.
- N1. Setting TMPDIR on dell (environment.d, feeder.env) is a host step for the dell host session; this item ships no host file. No change to the fast-check runner.

### File budgets

Per-file caps are in the card `line_caps` and the TSV. `host_ready.py` (794 lines) gets only the one field copy of D1a; the check lives in `host_ready_source.py`.

### Implementation

- host_ready_source.py. Add the record fields to the initial dict; after the repo disk measure, build the D1 command and plan, `measure('tmp', script, parse, steps=[...], **({'env_file': config['env_file']} if config.get('env_file') else {}))`, store fields, apply D3.
- tests/test_host_ready.py, tests/test_host_ready_path.py. The one healthy branch each (X4).
- genconfig.py, host_ready.py. The D1a field copies.
- rust/plangraph-cli/tests/host_sync_release.rs. The X3 CLI test.
- host_sync_release.rs. Lock error mapping, piped Cargo stderr scan and `failure_line` per D5, used by `run`; unit test in the existing `tests` module.
- docs/spawner/LE-DELL-TMP-QUOTA-FAILS-RELEASE-INSTALL-TESTS.md. The `tmp` check, its record fields, `tmp_floor_gb` and the two `tmp_low` reasons (the docs-gate fragment for the spawner source change).
- docs/placement-rules.md. Add `tmp` with `tmp_low` (tmpfs or below `tmp_floor_gb`, default 4) to the list of independent checks.
- docs/fleet-sync.md. One sentence on the host-fault message of `--install-release` and one on the `tmp_floor_gb` registry field.
- changelog one line.

## Open points for design approval

None.
