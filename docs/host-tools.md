
`pg-repo-sync` discovers direct children of `$HOME/code` with a `.git` entry,
including symlinks. Missing HOME or code directory is silent success. Repeat
`--root DIR` or `--repo PATH` to select checkouts; explicit labels stay as passed.
It fetches origin and silently fast-forwards a clean default branch. Alerts exit 1
and use only `detached`, `other-branch:NAME`, `tracked-changes`, `diverged`, or
`fetch-failed`, including defensive probe failures. No timer is installed here.

`--check-config --host ID --allow-name NAME --allow-email EMAIL` checks local
`core.bare` and identity overrides without writes; repeat allow flags as needed.
Absent overrides inherit the host identity. Drift reports host, path and escaped
values. Add `--sync` to also sync. All inherited `GIT_*` variables are removed.
Explicit paths require their own `.git` and matching Git top level, so a nested
path cannot fetch, merge or inspect config in its parent repository.
Every Git and systemd query is bounded to 60 seconds (lower with
`PG_REPO_SYNC_TIMEOUT_SECS`, minimum 1). Sync fetches origin with prune on every
run, regardless of disk usage; there is no `--apply` mode in this tool.
Running system and user service WorkingDirectory paths (including descendants
of a checkout) protect it from sync. Missing or unreadable unit information
fails closed with a `SKIP` report. Config-only inspection remains read-only.
