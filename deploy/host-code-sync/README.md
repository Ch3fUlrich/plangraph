Install the user service and timer in `~/.config/systemd/user/`, then enable
`plangraph-host-code-sync.timer`. The shared checkout must be clean and on main;
updates fetch main and fast-forward only. Dirty or diverged checkouts fail visibly
in the user journal. Adjust ExecStart if the binary or shared checkout lives elsewhere.

Use `pg-spawnd host-code-sync --hooks-checkout <path> --check` for a read-only drift
alarm against cached origin/main. The timer refreshes that reference on each sync.
Windows Task Scheduler can run the same command periodically.

The pre-push file-size check reads `.file-policy.json`, `.filesize-allow` and
`.filesize-baseline` from each pushed commit, so the shared hook checkout's own
policy does not override the branch policy. Existing installations must receive
this version of the hook/checker once before the tree policy applies.
