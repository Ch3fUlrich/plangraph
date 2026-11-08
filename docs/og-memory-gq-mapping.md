# Approved memory GQ mapping
D-4743 approves option (a) in [the contract](og-memory-contract.md), O1–O4; D-4745 requests this mapping. **Approved, not applied.** The offline schema is
`rust/og-memory/tests/fixtures/memory-v1-approved.pg`. Existing types and hub edges remain unchanged. This mapping writes only the authoritative companions, not legacy fact/hub projections; those cannot encode cross-project identity.
## Wiring scope
OG-MEM-IO-WIRING implements this against mock/fixture transports only until
OG-MEM-SCHEMA-APPLY is done by the operator. D-3895's live-write freeze holds. No live calls or schema application are authorized here.
## Remember/update publication
Read the complete project snapshot of both companions, pinned as in O5. Validate all fields and decode canonical `relationsJson` as the ordered array
of `{edge, target_slug}` pairs, preserving duplicates. Missing nullable V1 metadata is an error, not synthetic provenance. Validate kind against the four allowed kinds. Bind caller values, never interpolate identifiers or literals.
Compare complete envelopes, including source, actor and relations: equality with verified saved state skips both writes. Changed envelopes use the same composite key; `insert` is the keyed upsert, with no separate update keyword.
Dispatch this named GQ block through mutate, not query:
```gq
query rememberMemory($project: String, $kind: String, $slug: String,
                     $content: String, $version: I32, $sourceType: String,
                     $sourcePath: String, $sourceHash: String, $actor: String,
                     $relationsJson: String) {
  insert MemoryRecord {
    project: $project, kind: $kind, slug: $slug, content: $content,
    version: $version, sourceType: $sourceType, sourcePath: $sourcePath,
    sourceHash: $sourceHash, actor: $actor, relationsJson: $relationsJson
  }
}
```
Use `ifGraphCommit = Snapshot.graph_commit` from the preceding pinned read
(HTTP `Omnigraph-If-Graph-Commit` on `/mutate/if-graph-commit`). This is a request
precondition, not a GQ parameter. It applies even to first insertion.
Capture the exact successful `commit` object as the publication receipt;
`commit: null` and affected counts are not receipts. Never guess a future ID.
## Conditional saved-state follow-up
After publication, read back at the publication commit and verify the exact
MemoryRecord. Serialize that publication receipt losslessly into `$receiptJson`;
bind the validated per-fact superseded flag (false for a newly saved fact),
never derive it from Decision.status or Supersedes. Then dispatch:
```gq
query saveMemoryState($project: String, $kind: String, $slug: String,
                      $receiptJson: String, $superseded: Bool) {
  insert MemorySavedState {
    project: $project, kind: $kind, slug: $slug,
    receiptJson: $receiptJson, superseded: $superseded
  }
}
```
Use `ifGraphCommit` from that verified read (the publication commit). The
follow-up's own commit is not the stored publication receipt: storing it would
require an infinite receipt chain. Read back both records at the follow-up
commit and verify envelope, receipt and flag before returning `Saved`.
A 412 has no effects: re-read a complete pinned snapshot, recompute equality
and update, and use its new commit; never drop the precondition or replay the
old token. Respect the existing three-attempt save bound. If the publication
still matches and its receipt is known, retry only the conditional state write;
if another writer changed it, do not attach the old receipt to the new record.
Missing or stale state is incomplete, not a successful durable round trip.
Lost/malformed responses are `Unknown`; reconcile by readback, not blind replay.
If the publication receipt cannot be recovered and verified, fail closed as
`Unverified`; do not fabricate it or claim rollback.
O6 forbids atomic delete plus insert. Neither block deletes: keyed replacement
preserves identity and replaces the full scalar relation vector without edge
delete/reinsert. The follow-up state write handles publication of the receipt
only after the record commit; the two commits are explicitly **not atomic**.
Interrupted publication requires reconciliation before returning saved state.
Legacy hub-edge replacement is outside this mapping, not an assumed atomic
side effect. Recall must join companions by all three key fields and reject
incomplete/unreconciled pairs rather than returning legacy projections as V1.
