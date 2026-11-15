Fix itemless completion delivery so the parent Inbox receives completion once
without repeatedly attempting an inapplicable keeper item event. Record that
outcome explicitly in the keeper marker, independent of transport configuration.
Publish silent-run alert records as `kind=stall` with `stalled_at` and `noticed_at`,
preserving bridge completion payloads, episode deduplication and non-empty status.
