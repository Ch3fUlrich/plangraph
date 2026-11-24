A batch's review ACCEPT and part CI now carry over a rebase that leaves its patch-id unchanged
and touches none of its paths (`plangraph land-train evidence-assess`, `evidence-report`). The
CI reruns after more than three trains landed; a flake verdict voids no ACCEPT and never lets a
red required CI merge.
