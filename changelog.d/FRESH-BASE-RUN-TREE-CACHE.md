SHA-cached run trees now check their selected base against fetched origin/main
under the cache lock, refusing a stale base if another admission advanced the
tracking ref while selection waited. Callers whose admission already fetched
can pass `fetch=False` to reuse that fetch. Current-base reuse, offline tracking
ref selection, and local-main isolation remain unchanged.
