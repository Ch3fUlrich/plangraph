# L2 brief: build-L2-oc-harden   (lane E)
## Lanes, in order:
1. plangraph:RT-P1-OPENCODE-HARDEN PR #95 (lane/rt-p1-opencode-harden, head c9427a77): synthetic-id fixtures (no scrub allowlist widening), rename real-429-big-pickle.jsonl,
   the unpushed flatten/rustfmt commits from the predecessor build-L2-rtp1-predeploy-5/-6 worktree on this workstation (ask workst-L1-autoos for the path; reuse only commits with
   non-Claude Writer trailers, else redo via a writer); re-seat on the final head (2 families != writer). HIGH (harness env + auth files): + judge GO-REQ via build-L1.
2. plangraph:RT-P1-JEV-SHADOW PR #96 (lane/rt-p1-jev-shadow, head 2a8bb220): final-head seats, timeout cap 10 s, remove leftover QC axis. Done needs judge too.
