# L2 brief: build-L2-review-code — plangraph:RT-P1-REVIEW-CODE (standard; coding.vm or workstation; parent build-L1)
Rules: /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-rules-common.md (binding). TTL 4 h.
Done means: reviewer activity (tool-less gateway call + qodercli leg) choosing a family != writer family; prompt assembly with
vector block + answer-shape tail (port ops/assemble.py + ops/tail.md from branch build-L1 into the repo, site strings out); JSON verdict
pass/fix/halt parser (finish_reason, test_gaming) compatible with tools/seat_verdict.py on main; unit tests with recorded replies
(include the known failure shapes: bold VERDICT, missing ": 0 hits", finish=length empty, a tool-call reply from qodercli); 1 seat.
Language: Python module under src/plangraph/ (keeper side) unless design §4 says the worker calls it in Rust: decide per design, note why.
Worktree /home/s/code/plangraph-worktrees/review-code, branch lane/rt-p1-review-code from origin/main; draft PR.
