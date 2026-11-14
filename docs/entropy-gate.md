# Entropy ratchet

`pg-entropy` (pg-worker-core) checks the merge-base-to-head PR delta using Git
numstat, complete tracked text blobs and seven-day history relative to UTC now.
Run from a repository root with `--base REV --head REV --body-file FILE`.
Output includes additions, deletions, net LOC, class counts, ratio and every fix.
Exit 0 passes; 1 reports rule failures; 2 refuses unavailable evidence.

G1 caps changed lines at 400 unless a nonempty `Plan-Slice:` or
`Entropy-Waiver:` reason is supplied. G2 caps seven-day net growth per source,
test, plan-report and other class at 3%. G3 caps test additions at non-test
additions unless `Test-Heavy:` explains the excess; the tests/(src+rust) ratio
cannot exceed baseline, and every tracked test file must fit 400 lines.
G4 refuses new Markdown under plan/plans/report/reports or with a plan/report
filename prefix; required `changelog.d/` fragments are metadata. Test paths include tests/test directories, test_ prefixes,
_tests.rs and .test.ts/.test.js suffixes. Binary files contribute zero LOC.

`.entropy-baseline.json` holds numeric thresholds; existing values may only
shrink. A deleted/malformed baseline refuses. `--baseline` prints the current
committed ratio and standard thresholds for initial review; it writes nothing.
The checker is repository-independent and can be installed in another repository
with its own measured baseline and workflow. That repository is not bundled here.
The literal all-test-file rule also exposes existing files above 400 lines;
these must be split before this gate can pass. A Plan-Slice reason does not waive
G2, the global ratio, file-size, new Markdown or baseline rules.
