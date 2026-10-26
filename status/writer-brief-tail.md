# Writer brief tail (paste into every writer/fixer brief; build-L1)

Each commit message ends with ONE trailer block (no blank line inside it, nothing after it):
Plan-Item: plangraph:<KEY>
Writer: <harness>/<model> RUNID
(L1 fills RUNID; write the literal text RUNID.)
Never write Test-Removal, Gate-Edit or Hand-Edit trailers (D-2885): only L1 adds them, with a router D-id. If a test must be removed, renamed or loosened, stop and say so in the report; never do it.
Keep every test name the design names. Never rename a test.
Gates: python3 tools/ci_commit_gate.py check --base origin/main --head HEAD rc 0; python3 tools/mimo_path_sweep.py . rc 0.
Report: commits, per-step file:function, tests with counts, gate rcs, vector self-check. Then stop.
