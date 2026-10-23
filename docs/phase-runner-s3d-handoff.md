# Phase runner S3d

Worker completion is distinct from reviewed completion: a system lane claimed
through the worker-claim door enters `in_review` without pre-merge evidence only
while it belongs to a non-cancelled phase gate. The claim marker is epoch-bound,
so a manual claim cannot inherit this exception. Completion preserves the holder
and epoch, advances the review round, and clears the lease in both cache and replay.

Phase finish requires worker-complete (`in_review`) cards, validates the merge
commit returned by GitHub, and uses the existing evidence verifier/recorder for
each card's `commit+ci` evidence. A confirmed merge response is sufficient for the
immediate attach; the standalone recovery verb checks the phase PR's merged state.

Validation in WSL:

- Initial S3d tests: 8 failed, 6 passed.
- Implemented S3d tests: 14 passed.
- S3d, worker verbs, and evidence: 120 passed.
- Requested legacy regressions: 11 failed, 107 passed. Ten failures require the
  superseded `done`-card precondition; the GitHub adapter test supplies a merge
  response without the now-required SHA and expects no merge SHA in the result.
  These tests were not modified to conceal the contract conflict.
