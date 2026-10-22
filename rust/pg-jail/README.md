# pg-jail

`pg-jail` is a small Landlock filesystem jail. It applies an allow-list of
write and read paths, then execs a command. It is generic; callers choose the
paths.

## CLI

```
pg-jail [--rw PATH]... [--ro PATH]... [--deny-read PATH]... [--proc-self] -- CMD [ARGS]...
```

- `--rw PATH` full read/write/execute access.
- `--ro PATH` read/execute access.
- `--deny-read PATH` no rule for this path. It must not overlap an `--rw` or
  `--ro` path.
- `--proc-self` allow `/proc/<jail-pid>` and existing `/proc/cpuinfo`,
  `/proc/meminfo`, `/proc/stat`, `/proc/loadavg` files read-only. The PID is
  preserved across exec, so `/proc/self` works for the exec'd command, but not
  for a forked child's new PID. Sibling process directories remain denied.
  A legacy `--ro /proc` is ignored with this flag and rejected without it.

## Write jail

Allow writes only under the worktree and temporary directories, while keeping
the system readable:

```
pg-jail \
  --rw /worktree \
  --rw /tmp \
  --rw /dev/null \
  --ro /usr \
  --ro /lib \
  --ro /lib64 \
  --ro /bin \
  --ro /etc \
  --proc-self \
  --ro /dev \
  -- some-command
```

`/dev` is read-only here, so a command that writes to `/dev/null` needs it
listed under `--rw` as above.

## Read-deny

For a spawner that needs to run a command but keep a path unreadable, leave it
out of the allow lists and name it in `--deny-read`:

```
pg-jail \
  --rw /worktree \
  --rw /tmp \
  --ro /usr \
  --ro /lib \
  --ro /lib64 \
  --ro /bin \
  --ro /etc \
  --proc-self \
  --ro /dev \
  --deny-read /secrets \
  -- some-command
```

`--deny-read` is a validation guard, not a separate rule: Landlock only grants
access, so anything not listed in `--rw` or `--ro` is already denied.

## Fail closed

Missing `--rw` or `--ro` paths are errors. `--deny-read` paths that overlap an
allow entry (including generated procfs entries) are errors. Landlock ABI V2
is required, including `Refer` for rename/link across writable roots. If ABI V2
cannot be fully enforced, the process exits with `78`; there is no best-effort
bypass.

The resource-limit integration test is opt-in via
`PG_JAIL_TEST_RESOURCE_LIMITS=1` on hosts with a configured systemd user scope.
The default test run does not probe systemd or cgroups.

## Exit codes

- `64` usage error
- `78` Landlock unsupported or not fully enforced
- `126` other jail error
- `127` exec failed

## Why Landlock

Bubblewrap is not available on the target host, and unprivileged user
namespaces are blocked (measured). Landlock is an unprivileged LSM that works
there. Resource limits are handled separately, by a systemd user scope (see
`pg_worker_core::scope`).
