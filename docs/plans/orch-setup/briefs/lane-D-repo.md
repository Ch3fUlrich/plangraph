# Lane D: trailer hook, repo setup, AGENTS.md (plangraph-L2-su-repo)

STANDING RULES: `docs/plans/orch-setup/briefs/COMMON.md` (binding, read first). Design sections: 1 (verified facts), 6 (Trailer
hook, Repo setup).
Items, in order: `plangraph:SU-TRAILER`, `plangraph:SU-REPO`, then `plangraph:SU-AGENTSMD-PG`, `autoos:SU-AGENTSMD-AO`,
`server:SU-AGENTSMD-SRV` (the three in parallel). Starts once SU-CORE is merged.

## READ
- plangraph `src/plangraph/keeper/evidence_git.py:41-56,250-258` (trailer parse via `git show -s --format=%(trailers)`),
  `src/plangraph/keeper/evidence.py:276-282` (key match) and the item key format in `docs/PLAN-FORMAT.md`.
- AutoOS commits `ba90f77a` (broken) and `05a376d3` (fixed): `git log -1 --format=%B <sha>`.
- `CLAUDE.md` and `AGENTS.md` of plangraph, AutoOS and Server at origin/main.

## WRITE SCOPE
- plangraph: `rust/plangraph-cli/src/{trailer,repo,lint}/**` + tests + `[repo]` manifest section, `.github/workflows/*`
  (one trailers-check + claude-md lint step), `AGENTS.md`, `CLAUDE.md`.
- AutoOS: `AGENTS.md`, `CLAUDE.md`, one CI step.
- Server: `AGENTS.md`, `CLAUDE.md`, one CI step.

## SU-TRAILER spec
- `plangraph hook commit-msg <file>`:
  - collect every `Plan-Item:` line (case-insensitive key, as the keeper reads it) and remove it from the body;
  - put the lines at the head of the final trailer block, before `Co-Authored-By`;
  - if the message has no trailer block, append a blank line + the block;
  - verify with `git interpret-trailers --parse` that every Plan-Item is now a trailer;
  - refuse a malformed key with exit 1 and a one-line hint;
  - idempotent (a fixed message is unchanged).
- `plangraph trailers check <rev-range>`: for each commit, fail if `Plan-Item:` occurs in the body but not in `%(trailers)`.
- Fixtures: the ba90f77a message becomes the 05a376d3 shape; multi Plan-Item; Signed-off-by mixes; CRLF input; empty body.

## SU-REPO spec
- `plangraph lint claude-md [path]`: pass iff CLAUDE.md is absent and AGENTS.md exists, or CLAUDE.md is exactly `@AGENTS.md`
  (one trailing newline allowed) and AGENTS.md exists.
- `plangraph setup repo <path>`: run the lint (a failure reports `refused` with a hint and never moves text) and install the
  commit-msg hook.
  - Target: the repo's common hooks dir (worktrees share it).
  - If `core.hooksPath` is set, install a chain wrapper that runs the existing hook first, then ours.
  - Never replace a foreign hook: a foreign hook becomes the chained one, kept as `commit-msg.pre-plangraph`.
  - 2nd run skipped.

## SU-AGENTSMD-* spec (one PR per repo)
- Move every rule or orientation line from CLAUDE.md into AGENTS.md without loss; dedupe against AGENTS.md text.
- CLAUDE.md becomes `@AGENTS.md`. The plangraph AGENTS.md is new: the repo's rules plus a pointer to the fleet rules.
- The PR body carries a line-by-line mapping (old CLAUDE.md line -> AGENTS.md location) for the seats.
- The CI step runs `plangraph lint claude-md`. Until the release exists, CI builds the binary from the plangraph repo at a
  pinned sha.
