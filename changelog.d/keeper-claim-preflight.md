Keeper claim preflight now uses a configurable 30-second budget and reports the
step that exceeded it. Claims reuse verified work indexes within their TTL and
share in-flight refreshes, with bounded concurrency across independent checkouts.
