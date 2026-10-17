# Lane U1: UI-VERSION-STAMP, then UI-DEPLOY-AUTO

Read `COMMON.md` first.

## UI-VERSION-STAMP (2 h)

- In `render.py`, find the code sha in this order:
  1. env `PLANGRAPH_BUILD_SHA`;
  2. `git -C <package dir> rev-parse --short=10 HEAD`;
  3. `unknown`.
- Pass it to the page as a template value, not inside the snapshot JSON.
- Template: add `<meta name="plangraph-build" content="<sha>">`. The header shows `code <sha> · data <generated> · seq <head_seq>`.
  Today's `sha10` is the keeper head hash: keep it in the tooltip only, labelled "keeper head".
- V-SHELL-4: when `generated` is older than 15 min at view time, the stamp turns amber and shows its age. This runs in the page, not in
  render.
- Tests:
  - render with each of the three sha sources;
  - the meta tag is present;
  - the header text, with the amber state checked by a fake clock.

## UI-DEPLOY-AUTO (5 h, HIGH: 2 seats)

Constraint: CI runs on GitHub-hosted runners and cannot reach the LAN, and D-1377 forbids new schedules. So the live host pulls.
The existing render unit already runs every 5 min. Extend that run; do not add a timer.

1. Commit templates under `deploy/systemd/`:
   - `plangraph-render.service`;
   - the existing timer;
   - a drop-in template that sets `PYTHONPATH` to a FIXED live worktree path from env (`PLANGRAPH_LIVE_TREE`);
   - an nginx snippet for the page (`Cache-Control: no-cache`, gzip on).

   Ask build-L1 for the current unit text from the live host. Do not invent it, and do not copy host names into the repo.
2. A step at the start of the render run (`tools/live_follow.sh` or a Python module):
   - fetch `origin/main`;
   - CI runs only on PRs, so a merge commit has no check run of its own. Deploy the new head only if it came from a PR whose head
     sha has a green `tests` check:
     - `gh api repos/<repo>/commits/<sha>/pulls`, then the check-runs of that PR's head sha;
     - read-only token from the existing env file, never printed;
     - a commit without a PR needs its own green check run;
   - if so, fast-forward the live worktree, then render with `PLANGRAPH_BUILD_SHA=<sha>`;
   - else keep the current tree.

   A failed render keeps the old page (the existing `</html>` guard).
3. Post-deploy check: after a render, compare the page's `plangraph-build` with the newest green main sha. If they differ for more than
   30 min, write one line to the existing alert path (ask build-L1 which one; ntfy or the router inbox). Do not add a new service.
4. Docs: a short `deploy/README` section.
5. The live step: build-L1 asks the router for a GO. The run record shows main sha = page stamp within one render period.

Tests:
- the follow step with a fake git and a fake check API: green moves the tree, red or unknown keeps it, and render failure keeps the
  page;
- the unit templates contain no host-specific text (grep test).
