# Taildrop fetcher ownership

Decision (TAILDROP-SORT-HOME): **keep** the single host fetcher and owner-prefix
sorter in plangraph. Cross-host file delivery still uses Taildrop; sessions send
`owner__filename` and never drain the shared inbox themselves.

The Rust `plangraph taildrop-sort` command replaces the installed AutoOS script.
It uses PLANGRAPH_TAILDROP_ROOT (default `~/fleet/taildrop`) and holds one root lock
through fetch and sorting. Tailscale is invoked with `file get --wait=false
--conflict=rename`. Missing/failing Tailscale is a cheap, reported exit 0; a failed
move is nonzero and leaves the source for a later attempt.

Only regular file names are inspected. A lowercase owner starting with a letter
and containing only lowercase letters, digits and dashes becomes a directory;
`owner__rest` files there. Leading dot/dash in rest gets `_`. Other names, including
line breaks, go unchanged to `unsorted`; log entries flatten CR/LF. Clashes append
`.1`, `.2`, etc. Nothing opens, reads or hashes received file contents. Symlinks
are skipped in incoming and refused as destination directories. Hard-link plus
unlink files without replacing existing destinations, preserving the file mode.

On Linux, `setup apply` installs the service and minute timer from the embedded
manifest, and a second apply skips them. It does not activate host units. During
operator cutover, obtain a router decision, replace the existing timer/service,
reload and enable `taildrop-sort.timer`, verify one fetcher, then remove
`~/.local/share/autoos/taildrop-sort.sh`. Retain the old service/script outside the
public checkout until verification permits deletion; rollback restores that unit.
