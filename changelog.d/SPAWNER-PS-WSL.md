SSH ps hosts can set `wsl_distro` and a POSIX absolute `src` in
`ps-hosts.json` to query a tree inside WSL when Windows uses cmd.exe as
the SSH login shell. Distro names accept only letters, digits, `.`, `_`,
and `-`, with no leading dash; invalid entries retain the invalid-config row.
Hosts without `wsl_distro` keep their existing command behavior.
