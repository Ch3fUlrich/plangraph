# Proposed keeper CI config: GitHub slugs + one Forgejo project (PG-CI-FORGEJO)

**Status: proposal only — this diff is NOT applied.** It shows how an operator's
`keeper.json` would look once `commit+ci` can verify a project whose CI runs on
Forgejo Actions. Every value below is a placeholder: `OWNER` stands for the
organisations' owner name, the host is the reserved `.invalid` TLD, and the
token path is a generic absolute path. No real host, IP (except `127.0.0.1`,
which only ever appears in tests), user or home path appears here.

## Diff

```json
{
  "ci": {
    "plangraph": "OWNER/plangraph",
    "AutoOS": "OWNER/AutoOS",
    "great-wiki": "OWNER/great-wiki",
    "gen-analysis": "OWNER/gen-analysis",
    "Server": "OWNER/Server",
    "invest": "forgejo:OWNER/invest"
  },
  "forgejo_url": "https://forgejo.example.invalid",
  "forgejo_token_file": "/path/to/forgejo-ci-read.token"
}
```

* The five GitHub projects keep the plain `owner/repo` slug form and go through
  `gh api …/check-runs` exactly as before.
* `invest` uses the new `forgejo:<owner>/<repo>` syntax: the verifier reads
  Forgejo commit statuses instead (`GET {forgejo_url}/api/v1/repos/{owner}/{repo}/commits/{sha}/statuses?page=N&limit=M`,
  `Authorization: token <T>`).
* `forgejo_url` and `forgejo_token_file` must both be present before any
  `forgejo:` value can be looked up; with either missing the value fails
  closed in `CIAdapter.check_runs`.
* `forgejo_url` must be `https` (plain `http` is accepted only for
  `127.0.0.1`, which is what the hermetic tests bind).

## Live steps (operator, out of scope for this PR)

1. **Mint the token** on the Forgejo instance with read-only status scope and
   write it — nothing else — into `forgejo_token_file` on the keeper host.
   The file must be a regular file (a symlink is refused), non-empty, hold a
   single token, and be readable by the keeper user only (`chmod 600`).
2. **Edit the live `keeper.json`** by hand with the diff above (placeholders
   replaced by the real owner, host and token path), keeping the file mode at
   exactly `0600` — the loader refuses any other mode.
3. **Restart the keeper** so it reloads the config; the token file itself is
   re-read on every call, so rotating the token needs no restart, only the
   file rewrite.

Nothing in this proposal is written, minted or restarted by the PR that adds
`src/plangraph/keeper/evidence_forgejo.py`.
