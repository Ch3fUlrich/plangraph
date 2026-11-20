# Keeper-refused completion deliveries (plangraph:LE-KEEPER-REFUSED-CALL-LOOPS)

A completion the keeper refuses with a conflict (HTTP 409), or refuses twice with the same other non-transient 4xx status, is marked `keeper.delivered` as `refused: HTTP <status>` and is not re-driven by later ticks; 401, 403, 408, 425 and 429 stay retryable.
