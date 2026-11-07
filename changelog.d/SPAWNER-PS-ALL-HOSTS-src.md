Fix remote `ps` for source-tree deployments: hosts in `ps-hosts.json` may set
`src` to an absolute or `~/`-relative Python source directory (for example,
`~/code/plangraph/src`). SSH prefixes the query with `PYTHONPATH=<src>`;
hosts without `src` retain the existing command. Paths use a strict allowlist
without spaces or shell metacharacters; invalid paths produce the existing
`invalid-config` row.
