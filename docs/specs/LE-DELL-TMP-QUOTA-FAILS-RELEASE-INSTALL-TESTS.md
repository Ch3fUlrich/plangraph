# LE-DELL-TMP-QUOTA-FAILS-RELEASE-INSTALL-TESTS

Outcome. host-ready reports tmp_low when the TMPDIR of a host's launch environment is on tmpfs or has less free space than the floor, and a release install that fails on a disk quota or full disk says it is a host fault.

- plan_source. Keeper item plangraph:LE-DELL-TMP-QUOTA-FAILS-RELEASE-INSTALL-TESTS, 2026-11-27 16:16Z, priority 10.
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- card SHA-256. 9879a027cd5a2657975620c7e3e0821a84f047a8feb3143dfc74cba40b91728f
- tree SHA-256. 5c0da1ce031c80380e51f62b0b25e1ad468a3ff75b840e9b79b4f8afd337fb07
- evidence_after_deploy. Host step, not code (item How (1)): the dell host session sets `TMPDIR=/mnt/data/plangraph/tmp` (directory mode 0700) in `~/.config/environment.d/`, in `feeder.env` and in the spawner env file named by the host's `env_file`, then restarts the user units. Then: `python3 -m plangraph.spawner.host_ready --host dell --json` shows `tmp_dir` under `/mnt/data` and no `tmp:` failure, and one full fast-check run of a doc-only lane on dell passes `plangraph-cli/tests/host_sync_release.rs`. Record both in the item evidence.

## Checked against base

- host-ready measures memory, repo disk and work mounts, never the temporary directory: no `TMPDIR` read in the probe. [src/plangraph/spawner/host_ready_source.py:270-390]
- The probe runs remote steps through `command(check, script, steps, **plan)`; the `spawner()` helper shows how a step plan carries the host `env_file`, which is the spawner launch environment. [src/plangraph/spawner/host_ready_source.py:283-309,351-367]
- Every failure becomes one stable line `HOST-NOT-READY <host> <check> <reason>`. [src/plangraph/spawner/host_ready.py:785-790; docs/placement-rules.md:326-328]
- A release install error is printed as `FAIL release install: {error}`; the dell failure was `Disk quota exceeded (os error 122)` under a tmpfs `/tmp` with 9 GB free, so free bytes alone do not show a per-user tmpfs quota. [rust/plangraph-cli/src/host_sync_release.rs:235-255]
- `.docs-map` maps `src/plangraph/spawner/**` to `docs/SPAWNER.md`, which has no line headroom (668 lines against its 728 cap less 60); a new `docs/spawner/<ITEM>.md` fragment satisfies the docs gate instead. [.docs-map; tools/check_file_size.py:47-52; tools/check_changelog_docs.py:393-400]
- The shared test runner in `tests/test_host_ready.py` answers unknown scripts with empty output, so a new probe needs one branch there or every existing green test reports `invalid_response`. [tests/test_host_ready.py:30-51]

## Decisions

- D1. New host-ready check `tmp`, run for every reachable host right after the repo `disk` measure. Inner command (no `df`, so the existing `df -Pk` test branches do not match it): `sh -c 'd="${TMPDIR:-/tmp}"; printf "%s\n" "$d"; stat -f -c "%T %a %S" -- "$d"'`. When `config["env_file"]` is set, the step plan carries `env_file` (and the legacy script text sources it with `set -a && . <env_file>`), exactly as `spawner()` does, so the value is the launch environment's TMPDIR.
- D2. Parse: line 1 `tmp_dir`, line 2 `<fstype> <available blocks> <block size>`; `tmp_free_gb = blocks * size // 1024**3`. The record gains `tmp_dir`, `tmp_fs`, `tmp_free_gb` (all `None` when unmeasured). A parse error is `tmp: invalid_response`.
- D3. Failures (check `tmp`, reason starts with `tmp_low`): `tmp_low: <dir> on tmpfs` when `tmp_fs == "tmpfs"` (a tmpfs quota is not visible in free bytes; the dell incident had 9 GB free); otherwise `tmp_low: <dir> free <n> GB, floor <f> GB` when `tmp_free_gb < config.get("tmp_floor_gb", 4)`.
- D4. EXPERIMENT (rule 3.76) for the 4 GB default: the release install writes the host binaries of one train (a few hundred MB) plus test temp trees; 4 GB is roughly ten times that. Measure `tmp_free_gb` minima across hosts in host-ready records for 7 days and set `tmp_floor_gb` per host in `hosts.json` when needed.
- D5. Release install message. `run` maps an error whose `raw_os_error()` is 122 (EDQUOT) or 28 (ENOSPC) to `FAIL release install: host fault: no room to write (TMPDIR=<TMPDIR or /tmp>): {error}`; other errors keep today's text. The helper `fn failure_line(error: &io::Error, tmpdir: Option<&OsStr>) -> String` holds the mapping so it is unit tested without filling a disk. The integration test prints the CLI output on failure, so its message names the host fault without a test change.

## Entry points

- E1. `python3 -m plangraph.spawner.host_ready` (`probe_host` in `host_ready_source.py`).
- E2. `host-sync --install-release` (`host_sync_release::run`).

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | TMPDIR on tmpfs: record `tmp_fs == "tmpfs"`, failure `tmp: tmp_low: /tmp on tmpfs`, `ok` false, stderr/stdout line `HOST-NOT-READY fiction tmp tmp_low: /tmp on tmpfs`. | X1 |
| B2 | E1 | ext4 TMPDIR with 2 GB free and default floor: `tmp: tmp_low: <dir> free 2 GB, floor 4 GB`; with `tmp_floor_gb: 1` the host is ready. | X1 |
| B3 | E1 | With `env_file` configured the step plan carries `env_file`; a green host reports `tmp_dir`, `tmp_fs`, `tmp_free_gb` and no failure. | X2 |
| B4 | E2 | `failure_line` for os error 122 and 28 starts `host fault: no room to write (TMPDIR=`; for os error 2 it is the plain error text. | X3 |

## Verification

Write X1-X3 first; on the base X1/X2 see no `tmp` key or failure and X3 does not compile.

- X1. `tests/test_host_ready_tmp.py::test_tmpdir_on_tmpfs_or_below_the_floor_is_tmp_low` (parametrized; reuse `CONFIG` and `runner` from `test_host_ready`, answer the `stat -f` script per case).
- X2. `tests/test_host_ready_tmp.py::test_tmp_probe_runs_in_the_launch_environment` (record the argv/body the fake runner receives and assert `env_file` is part of the plan) and `::test_green_host_reports_tmp_fields`.
- X3. `rust/plangraph-cli/src/host_sync_release.rs::tests::quota_and_full_disk_are_named_host_faults`.
- X4. Held out: `tests/test_host_ready_path.py` and `tests/test_spawner_doctor.py` stay green; `tests/test_host_ready.py` passes with only the new runner branch (`elif 'stat -f' in script: output = '/tmp\next2/ext3 2250000 4096\n'`).

## Scope and reuse

- R1. Reuse `measure`, the step-plan transport with `env_file`, `fail` and the HOST-NOT-READY line format; no new transport, no sudo, no quota tool.
- N1. Setting TMPDIR on dell (environment.d, feeder.env, spawner env file) is a host step for the dell host session; this item ships no host file. No change to builds, the fast-check runner or the integration test file.

### File budgets

Per-file caps are in the card `line_caps` and the TSV. `host_ready.py` (794 lines) is not touched; the check lives in `host_ready_source.py`.

### Implementation

- host_ready_source.py. Add the record fields to the initial dict; after the repo disk measure, build the D1 command and plan, `measure('tmp', script, parse, steps=[...], **({'env_file': config['env_file']} if config.get('env_file') else {}))`, store fields, apply D3.
- tests/test_host_ready.py. The one runner branch in X4.
- host_sync_release.rs. `failure_line` per D5, used by `run`; unit test in the existing `tests` module.
- docs/spawner/LE-DELL-TMP-QUOTA-FAILS-RELEASE-INSTALL-TESTS.md. The `tmp` check, its record fields, `tmp_floor_gb` and the two `tmp_low` reasons (the docs-gate fragment for the spawner source change).
- docs/placement-rules.md. Add `tmp` with `tmp_low` (tmpfs or below `tmp_floor_gb`, default 4) to the list of independent checks.
- docs/fleet-sync.md. One sentence on the host-fault message of `--install-release`.
- changelog one line.

## Open points for design approval

None.
