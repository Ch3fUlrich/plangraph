### PLACEMENT-R5 cleanup-2
- Stream reachable-history batch output with object-sized overlapping blob windows, retaining at most one object because patterns have no finite maximum length. Reject truncated batch responses.
- Preserve SSH_AUTH_SOCK only for publish transport children (push, fetch and ls-remote); retain all other Git/SSH environment scrubs.
- Assert failed non-EXDEV installs leave no temporary pack and remove the redundant publish environment assertion.
- Coverage gap retained: missing-object responses are tested synthetically through validate_batch; resolve-level mutation between inventory and batch validation is not exercised. There is no injection seam between the synchronous inventory and batch calls; external mutation requires a timing-dependent competing process rather than a hermetic test. Successful resolve batching remains covered.
