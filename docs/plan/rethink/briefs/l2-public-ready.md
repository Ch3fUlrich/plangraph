# L2 brief: build-L2-public-ready (plangraph:PG-PUBLIC-READY), parent build-L1
Load skill `unattended-orchestration`; rules: build-L1 branch docs/plan/rethink/briefs/l2-rules-common.md (binding: D-1704/D-1714 model order, seats, no merge, no live steps, card <= 40 lines, RECYCLE-READY before 230k).
Host: WORKSTATION (tool-using L3 writers via autoos-agent spawn). If you run on coding.vm instead: D-437/D-1554, only tool-less writers/seats there (you apply returned diffs).

## Context (2026-10-17)
- Operator D-1715: github.com/Ch3fUlrich/plangraph is PUBLIC now, with its existing history. CI runs on GitHub-hosted runners.
- build-L1 gitleaks run (`gitleaks git --log-opts=--all --redact`) at main 2168134e found 19 `generic-api-key` hits, all synthetic test fixtures or plan text (tests/test_md_sync_*, test_keeper_*guard*, tests/fixtures/md_sync/*, docs/plans/2026-10-16-orchestration-setup.md:285, PROMPT.md:5). No real secret found.
- The repo has no LICENSE and no CONTRIBUTING.

## Done means: keeper `show plangraph:PG-PUBLIC-READY` done criteria. The history and public-flip parts are OUT of scope:
- the repo is already public, so a history rewrite would need a force-push and is an operator decision via routing. Do not plan one.

## Tasks (one closed L3 task each, disjoint files)
1. LICENSE (MIT, holder from git config of the repo owner; ask build-L1 if unclear) + CONTRIBUTING.md (short: tests, lanes, Plan-Item trailer, no secrets).
2. `.gitleaks.toml` allowlist for the fixture paths above, each with a reason comment; `gitleaks git --log-opts=--all` exits 0.
3. Site strings audit at the tip: hostnames, IPs, /home paths, coding.vm, routing-00, tailnet names. Move them to config/examples, or replace them with placeholders in docs/tests; report a names-list scan as before/after counts per pattern (paths only, no values).
4. `deploy/` site configs and any plan data: move out (to the private data repo path the router names) or replace with examples. List the files first and send build-L1 the list before deleting anything.
5. CI: DONE by build-L1 (PR #59 merged f3e2500e: ubuntu-24.04, no secrets, permissions read). Keep it so; never re-add self-hosted (S18).

## Review
HIGH item: 2 non-Claude seats from different families (D-1704 order; one runs the secret scan itself), then MERGE-READY to build-L1 with PR sha(s). build-L1 sends it to the judge.

## Report
Your DONE note gives: commits, PR numbers, scan outputs (counts), seat verdicts, refusals verbatim, what remains.
