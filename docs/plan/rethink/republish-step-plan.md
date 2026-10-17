# PG-REPUBLISH-FRESH step plan (build-L1, 2026-10-17, for routing GO; D-1751, D-1897)

Status: DRAFT. Nothing below has run. Needs routing GO per step 3 (public history replacement).

## Preconditions (all must hold before step 1)
- P1 follow-up merged: pre-push hook applies `[[scrub.allow_value]]` (judge note on #69). Without it the hook
  refuses step 3 (fail-closed on the allow-valued rust fixtures). We do not bypass it: no `--no-verify`,
  no `AUTOOS_PREPUSH_OVERRIDE`.
- P2 T1d merged (cli_verbs.py has no default port).
- P3 T2 manifest (full-dev, D-1751: dev stays public) has merged with 2 seats PASS. Today: BLOCKED, both seats FAIL.
  Excluding rust tls/harness/stall breaks cargo; it needs the deferred D-B scrub lanes.
- P4 private names list is present on this host (path private, never in a brief). `--no-words` is never used.
- P5 live trees that follow public main are paused (see Risks R1).

## Steps
1. Mirror (rollback source). `git clone --mirror https://github.com/Ch3fUlrich/plangraph.git <private>/republish/mirror-<ts>.git`.
   Record `git -C <mirror> show-ref` -> `<private>/republish/mirror-refs-<ts>.txt` (sha256 of the file + ref count;
   today 249 refs / 172 branches / 0 tags). Check the count against `git ls-remote origin | wc -l`.
2. Export. On a detached clone of main at sha S, run:
   `python3 tools/split/make_public.py --source <clone@S> --out <private>/republish/export-<ts> --words <list>`.
   That gives a fresh one-commit repo with export sha E. Then on the export itself:
   - `scrub_check.py <export> --rules manifest.toml --rules scrub_ci.toml --words <list>` -> `0 hit(s)`;
   - `gitleaks git <export> -c .gitleaks.toml` -> no leaks;
   - `pytest` (+ cargo test in CI) on the export tree -> green.
   Record E, S, the tool outputs (rule ids/counts only) and the file count.
3. Push (GO-gated). From the export repo with the hook installed (`git config core.hooksPath tools/hooks`):
   `git push --force-with-lease=main:<current public main sha> origin E:refs/heads/main`.
   Other public branches: deleted only after read-back. The list goes to routing first; open PRs close.
4. Read-back. `git ls-remote origin` -> `refs/heads/main = E`, branch list = the expected set.
   `gh api repos/Ch3fUlrich/plangraph/commits/main --jq .sha` = E. A fresh clone builds and scrubs clean.
5. Rollback. From the mirror: `git push --force origin 'refs/heads/*:refs/heads/*'` (+ tags if any).
   Then read-back against mirror-refs-<ts>.txt (all shas equal).

## Risks
- R1 live-follow: UI-DEPLOY-AUTO (z8 drop-in, live-render tree) follows public main. Unrelated history -> ff fails
  (or worse). Stop the plangraph-render deploy-auto timer before step 3, re-point the tree to E, restart, verify.
  md-sync and proxy run from the frozen proposal tree, so no impact there.
- R2 keeper evidence: commit+ci refs point at old main shas, which vanish from public. Verification ran at attach
  time, so this is OK unless something re-verifies later. The mirror keeps the shas (private archive).
- R3 lane branches and worktrees (172 branches) lose their base. All lanes must be merged or parked first
  (UI-BROWSER-TESTS WIP 6fd8de39, dev-stack-static, rtp1-predeploy, ...). Rebase onto E by patch.
- R4 hard to reverse once others clone or GitHub caches it (D-658 note). The mirror is the only rollback.

## Hook question (D-1897)
Yes: the current hook blocks step 3, because it ignores allow_value and scans the full tree when pushing a new
root. Fix = P1 (writer run 20261017-232410-...1b92de, then seats + merge), then export from a main that contains P1.
