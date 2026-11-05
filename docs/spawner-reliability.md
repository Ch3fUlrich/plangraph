# Spawner reliability

RL26 exact references accept registered ids or unique exact role-eligible models.
Ambiguous and fuzzy matches refuse, with deterministic bounded suggestions.
The read-only Python catalog bridge supplies id, model and roles; AdapterCatalog
caches successful reads for 60 seconds, keyed by state directory.

PLANGRAPH_LEG_REF overrides state/config.json leg_ref (string or role map).
Default and invalid switches are off; warn/shadow observes without changing args.
On validates references before routing or spawning and canonicalizes overrides.
Route is a preview that ignores the leg: Python route has no selector.
Spawn overrides forward the canonical id through Python spawn --leg.
Non-null diff_ref with implement refuses in every mode, including dry runs,
and names role review. No live switch is enabled by this change.

No external STD ACCEPT or historical live gate is claimed. Synthetic tests do
not satisfy either gate; independent acceptance remains required.
