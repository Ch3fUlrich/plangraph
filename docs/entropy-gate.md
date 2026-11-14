# Entropy ratchet

`pg-entropy` (pg-worker-core) checks the merge-base-to-head PR delta using Git
numstat, complete tracked text blobs and seven-day history relative to UTC now.
Run from a repository root with `--base REV --head REV --body-file FILE`.
Output includes additions, deletions, net LOC, class counts, ratio and every fix.
Exit 0 passes; 1 reports rule failures; 2 refuses unavailable evidence.

D-5881 voids G1: changed-line counts are reported and never cause a failure;
`Plan-Slice:` and `Entropy-Waiver:` are unnecessary. G2 caps seven-day net growth
per source, test, plan-report and other class at 3%. G3 caps test additions at non-test
additions unless `Test-Heavy:` explains the excess; the tests/(src+rust) ratio
cannot exceed baseline, and every tracked test file must fit 800 lines.
G4 refuses new Markdown under plan/plans/report/reports or with a plan/report
filename prefix; required `changelog.d/` fragments are metadata. Test paths include tests/test directories, test_ prefixes,
_tests.rs and .test.ts/.test.js suffixes. Binary files contribute zero LOC.

`.entropy-baseline.json` holds numeric thresholds; active values may only
shrink, except the D-5881 migration from exactly 400 to 800 test-file lines.
The legacy `max_changed_lines` field is informational and is not enforced.
A deleted/malformed baseline refuses. `--baseline` prints the current
committed ratio and standard thresholds for initial review; it writes nothing.
The checker is repository-independent and can be installed in another repository
with its own measured baseline and workflow. That repository is not bundled here.
The literal all-test-file rule also exposes existing files above 800 lines;
these must be split before this gate can pass. `Test-Heavy:` only waives the
addition ratio; it does not waive G2, the global ratio, file size, new Markdown
or baseline rules. The independent repository file-size gate remains unchanged.

The five PR fixtures now yield pass/pass/fail/fail/pass (ordinary, over-size,
test-heavy, new plan Markdown, 401-line test), with an 801-line refusal fixture.
The standalone workflow is advisory for merging: it is absent from `ci-gate`
and the required-job lists; G2-G4 failures still produce a failing entropy job.
