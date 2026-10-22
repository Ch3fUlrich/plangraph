# pg-jail

`pg-jail` is a small Landlock filesystem jail. It applies an allow-list of
write and read paths, then execs a command. It is generic; callers choose the
paths.

## CLI

```
pg-jail [--rw PATH]... [--ro PATH]... [--deny-read PATH]... [--best-effort] -- CMD [ARGS]...
```

- `--rw PATH` full read/write/execute access.
- `--ro PATH` read/execute access.
- `--deny-read PATH` no rule for this path. It must not overlap an `--rw` or
  `--ro` path.
- `--best-effort` accept a partially enforced ruleset.

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
  --ro /proc \
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
  --ro /proc \
  --ro /dev \
  --deny-read /secrets \
  -- some-command
```

`--deny-read` is a validation guard, not a separate rule: Landlock only grants
access, so anything not listed in `--rw` or `--ro` is already denied.

## Fail closed

Missing `--rw` or `--ro` paths are errors. `--deny-read` paths that overlap an
allow entry are errors. If the ruleset is not fully enforced and
`--best-effort` is not set, the process exits with `78`.

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
