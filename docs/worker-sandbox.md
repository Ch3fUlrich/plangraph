# Worker attempt sandbox

An attempt runs `opencode run` as a child of the worker, with cwd set to the
attempt worktree. Confinement combines a filesystem jail, resource limits, and
standalone agent mode.

Worker CI checks formatting and warning-free Clippy before running the attempt
tests against a Temporal dev server; extracted result helpers retain their
function documentation in the included source file.

## What confines an attempt

- Filesystem: `pg-jail` applies a Landlock allow-list before exec. Writes are
  allowed only under the worktree, the attempt's private HOME, and private tmp; system
  paths are read-only where needed. Missing allow-list paths fail closed.
  The ruleset is built for Landlock ABI V3, which is the ABI that added
  `truncate` handling; a kernel that cannot enforce V3 is refused with exit 78
  rather than jailed at a weaker ABI.
- Resources: the harness runs the jailed child inside a transient
  `systemd-run --user --scope` with `MemoryMax` and `TasksMax`.
- Agent mode: invoke `opencode run --standalone`. Without `--standalone`, the
  default agent delegates tools to a shared host service outside the jail.
- Attach leg: refused by default. `PG_ALLOW_ATTACH` is the explicit opt-in;
  leave it unset for normal attempts. `PG_ALLOW_ATTACH` is a global switch, not
  per-card: a worker started with `PG_ALLOW_ATTACH=1` serves attended cards only.

## Environment knobs

| Variable | Effect |
| --- | --- |
| `PG_JAIL_BIN` | Absolute path to an existing `pg-jail` helper file; canonicalized before storage. Relative, missing, and non-file paths are refused. |
| `PG_ATTEMPT_MEMORY_MAX` | Value for the scope's `MemoryMax` property. |
| `PG_ATTEMPT_TASKS_MAX` | Value for the scope's `TasksMax` property. |
| `PG_ALLOW_ATTACH` | Permit the attach leg; default is refused. |
| `PG_CARD_ALLOWLIST` | Absolute UTF-8 file path, one card key per line; blank and `#` comment lines ignored. Unreadable or empty-of-keys files stop startup. Unset refuses all cards. Unlisted keys are refused before claim and attempt setup. |

Set `PG_CARD_ALLOWLIST` before deployment. When unset, the worker still starts
and logs a warning, but terminally refuses every card claim; a healthy poller
does not imply that cards can run.

Cross-worker exclusivity uses keeper claim arbitration. Each claim sends
`merge_owner_id()`, the process-stable random worker identity also used for merge
locks; an empty claim is a non-retryable refusal. A process-global
drop guard prevents overlapping activity attempts for the same card key and
releases ownership on return or unwind. Only admission errors are non-retryable;
filesystem permission errors retain the ordinary retryable attempt failure.
Typed admission reasons map to terminal `card_not_allowlisted`,
`card_not_claimable` (empty claim), and `card_attempt_overlap` failures.
The worker loads the environment allowlist once at startup and passes an immutable
Config to claim and attempt entry points; unloaded Configs refuse admission.
Programmatically supplied allowlists take precedence over the environment.

Each attempt creates its own private temporary directory under the system temp
directory, removed on drop alongside its private HOME. Only that temporary
directory is writable in the jail, not the shared system temp root; the child
receives its path as `TMPDIR` after environment confinement.

S1 supplies bounded output-redaction helpers: history keeps the last 64 KiB;
question text, defaults, and risk tags keep the first 64 KiB after redaction.
Incomplete prefixed tokens can continue across multiple lines until their
minimum token length is reached. S3 wires the helpers to history/projection
writes; seeded harness secrets never reaching either is the S1+S2+S3 stack's
end-state invariant, not an S1 end-to-end guarantee.

Auth staging excludes `.git-credentials`, `.gitconfig`, `.ssh`, `.netrc`,
`.npmrc`, `.pypirc`, `.aws`, `.docker`, `.gnupg`, `hosts.yml`, `credentials`,
and `.config` at both supported directory levels. Ordinary `agent-auth` and
`sub/agent-auth` survive.
The nested `stage_auth_into` exclusion was introduced in commit `901b141f`;
the dual-level staging assertions regress if either exclusion is removed.
Tests prove parent Git credential lookup succeeds
with a store helper, while confined harness/check lookup fails for missing
authentication (Git cannot obtain a password), the positive
control secret is absent from both output streams, and credential environment
variables are absent. This is not a
network prohibition or protection from arbitrary other credential filenames.

The local devstack e2e test profile (`tests/e2e/rtp1_local_util.py`) selects a
built `pg-jail` beside the worker binary when present, otherwise explicitly sets
`PG_JAIL=off`. This fallback is test-profile-only, never a worker default;
explicit caller configuration is preserved.

## WSL hardening

WSL defaults can expose host drives and host executables to the worker distro.
In the worker distro's `/etc/wsl.conf`, set:

```ini
[automount]
enabled=false

[interop]
enabled=false
```

This is an operator step. Run `wsl --shutdown` to apply it. `wsl_findings`
reports these defaults from the conf text; `is_wsl` identifies a WSL kernel
from `osrelease`. The worker checks both at startup: on a WSL kernel it reads
`/etc/wsl.conf` (`None` when absent) and prints one
`warning: WSL <finding>` line per remaining finding; off WSL it prints nothing.

## Verification

- Landlock is in the kernel LSM list: `grep -w landlock /sys/kernel/security/lsm`.
- The jail denies an out-of-tree write and permits an in-tree one with a small
  `pg-jail --rw . --rw /tmp -- true`-style probe.
- `truncate(2)` on a path outside the allowed roots fails with `EACCES` and
  leaves the file at its original size, while the same call inside an `--rw`
  root succeeds (ABI V3 handling).
- The scope accepts the configured limits:
  `systemd-run --user --scope -p MemoryMax=... -p TasksMax=... true`.
- The agent command contains `--standalone`.

## Limits

- Landlock restricts filesystem access only.
- File metadata calls outside the allow-list are not covered: Landlock has no
  access right for `chmod(2)`/`fchmod(2)` or for the `utime(2)`/`utimes(2)`/
  `utimensat(2)` family (and no ABI adds one), so a jailed process that can name
  a path outside the allowed roots can still change that file's mode or
  timestamps where ordinary file ownership allows it. The jail does not claim
  coverage for these calls; do not treat file modes or mtimes outside the
  worktree as protected by this layer.
- Procfs is restricted: the worker passes `--proc-self`, which allows read-only
  only `/proc/<own pid>` (so `/proc/self`) plus `/proc/{cpuinfo,meminfo,stat,loadavg}`.
  A sibling same-user process's `/proc/<pid>/environ` is refused (covered by the
  escape tests; node starts under this policy). The jail cannot hide the attempt's
  own `/proc/<pid>`, so keep secrets out of the attempt's own environment and argv.
- The library default (`JailConfig::from_env`) is an alias of
  `required_from_env`, so both fail closed: with `PG_JAIL_BIN` unset it returns
  an error and no attempt runs unjailed, unless the test-only override
  `PG_JAIL=off` **and** `PG_TEST_ALLOW_UNJAILED=1` are both set. An empty or
  non-absolute `PG_JAIL_BIN` is an error too. Worker startup calls
  `required_from_env` and attempt setup calls `from_env`, same policy.
- Landlock is filesystem-only and the attempt runs as the worker's uid, so this
  layer does not isolate same-user processes: signals to, and `ptrace` of, the
  worker or a sibling attempt are NOT blocked here (ptrace further subject to
  the host's `yama` `ptrace_scope`). Keep secrets out of the worker's
  environment where possible.
- There is no network egress control in this layer.
- The kernel must list Landlock in its LSM set and enforce Landlock ABI V3
  (or newer); otherwise `pg-jail` fails closed with exit 78.
- The systemd scope limits CPU, memory, and tasks, not files or network.
- Without `--standalone`, tool calls can escape the jail through the shared host
  service.

## Host facts

The admission policy guard runs exactly three production checks per workspace:
no default features, defaults, and all named features except test-only features
and their aliases. Metadata rejects defaults that transitively enable a forbidden
feature; a downstream compile-fail probe verifies the bypass remains unavailable.
Run `python3 tools/probe_admission_policy_guard.py` for the two negative probes
(a caller gated on a disabled default feature and a forbidden default alias).
Missing tools or paths fail rather than skip. The rust-worker job passed in
235.1 seconds locally; its guard took 85.59 seconds (207.2 seconds standalone).

Bubblewrap is not available and unprivileged user namespaces are blocked on the
target host. Landlock and `systemd-run --user --scope` work there, so this layer
uses them.
