Implemented offline-tested, pinned HTTP companion recall with strict V1 reconstruction; publication, receipt reconciliation, and binary activation remain deferred at the recall-first cut.
Resolve one shared commit before both companion queries, cache approved validation capabilities, reject unpinned or extra states, and refuse publication as terminal Unavailable without dispatch.
Cover missing and empty RESOLVE pins before companion dispatch and share the approved schema fixture between production validation and tests.
Report interrupted or unverified follow-up as non-retryable PartialPublication with the record commit, cover conditional and snapshot failures offline, and verify the unconfigured binary transport refuses publication as Unavailable.
Restore a production-owned schema with parsed capability parity against the approved fixture and explicitly reject missing or empty RESOLVE commits before companion dispatch.

Cut 2: Add offline-tested conditional MemoryRecord publication and verified real-receipt saved-state follow-up for remember/update. Preserve opaque record commits in the blocked ledger; verify written fields by key while ignoring extra server columns.
Activate the binary HTTP transport with runtime OG_MEMORY_BASE_URL and OG_MEMORY_TOKEN_FILE configuration; missing configuration or unreadable credentials fail closed with named errors without exposing token values. Offline binary tests cover activation and refusal. No live writes (D-3895 freeze).
