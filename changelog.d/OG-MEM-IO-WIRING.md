Implemented offline-tested, pinned HTTP companion recall with strict V1 reconstruction; publication, receipt reconciliation, and binary activation remain deferred at the recall-first cut.
Added fixture-only conditional MemoryRecord publication and verified real-receipt saved-state follow-up for remember/update; interrupted-publication reconciliation and configured binary activation remain deferred at the remember cut.
Resolve one shared commit before both companion queries, cache approved validation capabilities, and reject unpinned or extra states; HTTP publication uses conditional record and saved-state writes.
Cover missing and empty RESOLVE pins before companion dispatch and share the approved schema fixture between production validation and tests.
Report interrupted or unverified follow-up as non-retryable PartialPublication with the record commit, cover conditional and snapshot failures offline, and verify the unconfigured binary transport refuses publication as Unavailable.
Restore a production-owned schema with parsed capability parity against the approved fixture and explicitly reject missing or empty RESOLVE commits before companion dispatch.
