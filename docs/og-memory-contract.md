# Durable memory envelope v1

Offline `validate` alone constructs `Validated`, retaining source/actor provenance.
| Kind / target type | Property values | Project hub edge |
| --- | --- | --- |
| Decision | slug ← slug; title ← slug; rationale ← content; status ← accepted; date ← empty string | DecidedIn |
| Rule | slug ← slug; statement ← content; severity ← must | ConstrainsProject |
| Convention | slug ← slug; name ← slug; example ← content | AppliesTo |
| Preference | slug ← slug; statement ← content; scope ← project | none |
| Project hub | slug ← project | — |
`global` is allowed; slugs use lowercase ASCII letters/digits and single internal dashes.
Task/routing-* always fail; `sync-memory-graph --load` alone writes routing records.
Live-schema capabilities gate the sole V1 property table; Preference refuses hub links.
All text uses public worker-core redaction; refusals never retain rejected input.
