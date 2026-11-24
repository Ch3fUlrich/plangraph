A batch's review ACCEPT and part CI now carry over a rebase that leaves its patch-id unchanged
and touches none of its paths (`plangraph land-train evidence-assess`, `evidence-report`). The
CI reruns after more than three trains landed; a flake verdict voids no ACCEPT and never lets a
red required CI merge.
The patch identity keeps whitespace, main's touched paths come from its commits (a touch and a revert still
voids), a verified carry is persisted (`--carry-out`) and consumed by `land-train build --carries`, and the
log counts each rebase once per batch and head with the landed batches as denominator.
A carry is assessed again at admission against the build base and the landed trains, merges count by what
they changed against main, and only a head that does not contain the old one counts as a rebase.
Parts read from --parts carry nothing of their own (an embedded carry is dropped; only a carry assessed again at admission is taken), a merge counts by what it changed against all its parents, and only a head rewritten onto a new base counts as a rebase.
