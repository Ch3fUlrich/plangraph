# The public plangraph repo and its private data repo

plangraph is split in two (M2c):

| repo | contains | history |
|---|---|---|
| **public** (`plangraph`) | code, tests with synthetic fixtures, vendored JS + licences, the docs listed in `tools/split/manifest.toml`, `examples/` | history cut pending (D-1966): the public history is not yet a fresh single commit; the cut and force-push are operator-only |
| **private data** | the keeper DB, plan snapshots, decisions, questions/answers, batch files, the keeper config, the private word list and private scrub rules | its own, never published |

Nothing site-specific lives in the public repo: no hostnames, IPs, e-mail addresses, keys, home
paths, router or project names, plans or decisions. The code reads every such value from the
keeper config (below), so the two repos only meet at run time.

## What the public repo contains

Exactly the paths allow-listed in `tools/split/manifest.toml` (`[public]`): `src/`, `tests/`,
`tools/`, `vendor/`, `examples/`, the listed `docs/*.md`, and, when present, `LICENSE`,
`README.md`, `pyproject.toml`, `.gitignore`. Entries under `exclude` are removed (real-data
fixtures, private tools). Anything matching `[deny]` that is still selected makes the build
**refuse**; it is never dropped silently, because a deny hit means the allow-list is wrong.

`examples/` holds two clearly fictional projects (`plans/orchard.yaml`, `plans/lighthouse.yaml`,
plan-v3 import format, one cross-project dependency), one batch (`batch/demo-batch.yaml`, source
pinned by sha256) and a rendered page (`rendered/demo.html`, from `snapshot.json`). Regenerate with
`python3 tools/split/build_examples.py` (`--check` verifies they are current).

## How the private data repo plugs in

The keeper reads one JSON config file (mode `0600`, see `docs/KEEPER.md`). Keep it in the private
repo and point the keeper at it; every site-specific path is a field of that file:

| config field | points at (in the private repo or elsewhere on the host) |
|---|---|
| `db` | the SQLite keeper DB |
| `snapshot` | where `plan.snapshot.json` is rewritten after every write |
| `repos` | `{project: working-tree path}` for the git verifier |
| `runs_dir` | `run-record` job directories |
| `decisions_file` | the `DECISIONS.md` the `decision` verifier reads |
| `ci` | `{project: "owner/repo" or "none"}` |
| `batch_roots` | directories `close_on_decision` may read batch files from |
| `bind`, `default_lease` | listen address, claim lease seconds |

Typical layout: clone the public repo as `plangraph/`, the private repo as `<data-repo>/`
(`keeper.json`, `keeper.db`, `plan.snapshot.json`, `batches/`, `DECISIONS.md`, `scrub/words.txt`,
`scrub/rules.toml`), and start the keeper with the private config path. Batches are applied with
`tools/apply_batch.py BATCH.yaml --url ... --token-file PATH` (the token file stays outside both
repos).

## Running the checks

All commands run from a checkout of the code; none prints a matched value, only
`path:line: rule`.

```
# 1. scan any tree (exit 0 clean, 1 hits, 2 usage)
python3 tools/split/scrub_check.py TREE \
    --rules tools/split/manifest.toml --rules PRIVATE/scrub/rules.toml --words PRIVATE/scrub/words.txt

# 2. dry run of the whole pipeline: stage the allow-list in a temp dir, deny + scrub (+ gitleaks), delete
python3 tools/split/make_public.py --source . --check \
    --rules PRIVATE/scrub/rules.toml --words PRIVATE/scrub/words.txt

# 3. build the fresh public repo (refuses on any hit and writes nothing; --out must not exist or be empty)
#    PRECONDITION for the real public push: gitleaks is installed and on PATH (no --no-gitleaks, no
#    "gitleaks missing" warning in the output), and the private rules file is passed with --rules.
python3 tools/split/make_public.py --source . --out ../plangraph-public \
    --rules PRIVATE/scrub/rules.toml --words PRIVATE/scrub/words.txt
```

Rules checked: `hostname` (built-in internal suffixes + your `hostname_patterns` regexes), `ipv4`,
`ipv6`, `email`, `token-shape` (known key prefixes, PEM headers, JWTs, `token = "..."`),
`high-entropy`, `credential-shape` (the `credential_guard` word/secret pair), `home-path`,
`private-word` (your word list, also matched against file names), `symlink` (never allow-listable).
`ipv6` covers full and compressed forms (a compressed form needs a digit, so `db::add` is not a hit;
a subscript like `a[::1]` is not either). `high-entropy` flags mixed-case+digit tokens, long
lower- or upper-case+digit tokens, and plain hex only on a line that names a secret/token/key (so
commit and file hashes are fine). `home-path` covers `/home`, `/var/home`, `/Users` and
`C:\Users` (`C:/Users`). Deny and `[[scrub.allow]]` globs are case-insensitive. Binary files are not
scanned (they are listed).

A path that contains a private word is never printed: the line shows `<path-hash:XXXXXXXXXXXX>`
(first 12 hex of the sha256 of the relative path) instead, for `private-word` hits on paths and for
the "binary file not scanned" notes. On your own terminal, `scrub_check.py TREE --resolve XXXXXXXXXXXX`
prints the real path. gitleaks findings are reduced to tree-relative `file:line: gitleaks:RULE`
(`<outside>` for anything not under the tree). `gitleaks` is run when it is on `PATH` (or `--gitleaks PATH`); if it is
missing a warning says so and the scrub still runs. Use `--no-gitleaks` to skip it.

`rules.toml` (public defaults live in the manifest's `[scrub]` section):

```toml
[scrub]
hostname_patterns = ["build[0-9]+\\.example-lab"]   # regexes for your real hostnames/domains
allow_email_domains = ["example.org"]
allow_home_names = ["alice"]
[[scrub.allow]]                                      # reviewed exceptions: path glob + rules
path = "tests/test_thing.py"
rules = ["token-shape"]
```

### The private names list (`--words`)

`make_public.py` **requires** `--words PATH` (also with `--check`): without it the run exits 2 and
writes nothing. A missing, unreadable, non-UTF-8 or empty list (no entry besides comments/blank
lines) is refused the same way; a bad `re:` line is reported as `line N` only, never echoed.
`--no-words` skips the check for local development; it prints a warning and must never be used
for the real public build. `scrub_check.py` keeps `--words` optional (a warning says the
`private-word` rule is then inactive).

Format: one entry per line, `#` comments, blank lines ignored. A plain entry is matched
case-insensitively as a whole token: it must not be glued to a letter or digit on either side
(`_`, `-`, `.`, `/` count as boundaries), so `ab` hits `AB`, `x-ab_y`, not `abc` or `xab`. An entry
starting with `re:` is a case-insensitive regex. The same list is matched against file names.
What belongs in it (values only in the file, never in this repo): session/principal names, host
names and domains, the GitHub owner, operator name/e-mail, claude.ai artifact ids (one `re:` for
the URL shape), private repo names and paths, site ports, timezone. The file stays in the private
repo (`scrub/words.txt`); its content never reaches output: hits are `path:line: private-word`,
paths containing a word are shown as `<path-hash:...>`, counts only.

A value that is functional in the public code (for example the keeper's default port) is not
removed from the list: add a reviewed `[[scrub.allow]]` entry with `rules = ["private-word"]` for
that exact path in the private `rules.toml`.

### gitleaks and the fixture allowlist

`make_public.py` runs gitleaks with `tools/split/gitleaks.toml` (`--leaks-config` overrides):
the default rules plus a reviewed allowlist of synthetic, credential-shaped strings in test
fixtures. Each entry needs the exact test path **and** the exact fake value (`condition = "AND"`,
`targetRules = ["generic-api-key"]`; without `targetRules` 8.30.1 was observed to apply the paths alone), and
`tests/test_split_make_public.py` checks that only `tests/` paths appear in it. Never add a
non-test path there.

## Before publishing

1. Run step 2 with your private rules/words; fix or allow-list every hit (an `allow` entry is a
   claim that the value is synthetic: review it).
2. Real-data fixtures and private tools go in `exclude`; project/router names in test fixtures
   are caught by the private word list: rename them in the fixtures.
3. Add `LICENSE` and `README.md` to the source checkout (the manifest lists them as optional).
4. Run step 3, inspect `git -C ../plangraph-public log --stat` (one commit), then push it yourself.
