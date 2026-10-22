# Worker attempt sandbox

An attempt runs `opencode run` as a child of the worker, with cwd set to the
attempt worktree. Confinement combines a filesystem jail, resource limits, and
standalone agent mode.

## What confines an attempt

- Filesystem: `pg-jail` applies a Landlock allow-list before exec. Writes are
  allowed only under the worktree, the attempt's private HOME, and tmp; system
  paths are read-only where needed. Missing allow-list paths fail closed.
- Resources: the harness runs the jailed child inside a transient
  `systemd-run --user --scope` with `MemoryMax` and `TasksMax`.
- Agent mode: invoke `opencode run --standalone`. Without `--standalone`, the
  default agent delegates tools to a shared host service outside the jail.
- Attach leg: refused by default. `PG_ALLOW_ATTACH` is the explicit opt-in;
  leave it unset for normal attempts.

## Environment knobs

| Variable | Effect |
| --- | --- |
| `PG_JAIL_BIN` | Path to the `pg-jail` helper binary. |
| `PG_ATTEMPT_MEMORY_MAX` | Value for the scope's `MemoryMax` property. |
| `PG_ATTEMPT_TASKS_MAX` | Value for the scope's `TasksMax` property. |
| `PG_ALLOW_ATTACH` | Permit the attach leg; default is refused. |

## WSL hardening

WSL defaults can expose host drives and host executables to the worker distro.
In the worker distro's `/etc/wsl.conf`, set:

