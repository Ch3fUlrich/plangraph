
`pg-repo-sync` discovers direct children of `$HOME/code` with a `.git` entry,
including symlinks. Missing HOME or code directory is silent success. Repeat
`--root DIR` or `--repo PATH` to select checkouts; explicit labels stay as passed.
It fetches origin and silently fast-forwards a clean default branch. Alerts exit 1
and use only `detached`, `other-branch:NAME`, `tracked-changes`, `diverged`, or
`fetch-failed`, including defensive probe failures. No timer is installed here.
