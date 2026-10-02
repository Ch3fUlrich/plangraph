# The public plangraph repo and its private data repo

plangraph is split in two (M2c):

| repo | contains | history |
|---|---|---|
| **public** (`plangraph`) | code, tests with synthetic fixtures, vendored JS + licences, the docs listed in `tools/split/manifest.toml`, `examples/` | fresh: one commit by `plangraph contributors` |
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

Typical layout: clone the public repo as `plangraph/`, the private repo as `plangraph-data/`
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
python3 tools/split/make_public.py --source . --out ../plangraph-public \
    --rules PRIVATE/scrub/rules.toml --words PRIVATE/scrub/words.txt
```

Rules checked: `hostname` (built-in internal suffixes + your `hostname_patterns` regexes), `ipv4`,
`ipv6`, `email`, `token-shape` (known key prefixes, PEM headers, JWTs, `token = "..."`),
`high-entropy`, `credential-shape` (the `credential_guard` word/secret pair), `home-path`,
`private-word` (your word list, also matched against file names), `symlink`. Binary files are not
scanned (they are listed). `gitleaks` is run when it is on `PATH` (or `--gitleaks PATH`); if it is
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

The word list is one word per line (`#` comments; `re:` prefix for a regex, case-insensitive,
word-bounded). The list itself stays in the private repo.

## Before publishing

1. Run step 2 with your private rules/words; fix or allow-list every hit (an `allow` entry is a
   claim that the value is synthetic: review it).
2. Real-data fixtures and private tools go in `exclude`; project/router names in test fixtures
   are caught by the private word list: rename them in the fixtures.
3. Add `LICENSE` and `README.md` to the source checkout (the manifest lists them as optional).
4. Run step 3, inspect `git -C ../plangraph-public log --stat` (one commit), then push it yourself.
