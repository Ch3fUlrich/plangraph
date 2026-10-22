# Worker attempt sandbox

An attempt runs `opencode run` as a child of the worker, with cwd set to the
attempt worktree. Confinement combines a filesystem jail, resource limits, and
standalone agent mode.

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

Each attempt creates its own private temporary directory under the system temp
directory, removed on drop alongside its private HOME. Only that temporary
directory is writable in the jail, not the shared system temp root; the child
receives its path as `TMPDIR` after environment confinement.

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
from `osrelease`.

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
- The library default (`JailConfig::from_env`) runs unjailed when `PG_JAIL_BIN` is
  unset; the fail-closed policy is enforced at worker startup, so any other
  entrypoint must call `required_from_env` itself.
- There is no network egress control in this layer.
- The kernel must list Landlock in its LSM set and enforce Landlock ABI V3
  (or newer); otherwise `pg-jail` fails closed with exit 78.
- The systemd scope limits CPU, memory, and tasks, not files or network.
- Without `--standalone`, tool calls can escape the jail through the shared host
  service.

## Host facts

Bubblewrap is not available and unprivileged user namespaces are blocked on the
target host. Landlock and `systemd-run --user --scope` work there, so this layer
uses them.
