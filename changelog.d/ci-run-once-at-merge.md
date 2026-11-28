CI runs the full suite exactly once, on the merge candidate into main: a land/train-* head, a pull
request labelled ci-full at merge time, or a manual dispatch. batch/* heads, parts, lanes, the merge
queue and the push to main run the affected selection of their own diff (the push from the pushed-over
sha); the nightly schedule is gone. Python jobs use tools/affected_tests.py on every non-full event; the
Rust jobs test only the changed crates and their dependents (select output rust-packages). The browser
job caches the Chromium download in its own bounded, retried step, saved right after a good install.
