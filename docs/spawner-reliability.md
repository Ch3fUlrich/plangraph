# Spawner reliability

RL26 exact references accept registered ids or unique exact role-eligible models.
Ambiguous and fuzzy matches refuse, with deterministic bounded suggestions.
The read-only Python catalog bridge supplies id, model and roles; AdapterCatalog
caches successful reads for 60 seconds, keyed by state directory.

PLANGRAPH_RELIABILITY_LEG_REF overrides $XDG_CONFIG_HOME/plangraph/reliability.json
leg_ref (string or role map); default root is ~/.config (APPDATA on Windows), independent of state.
Default and invalid switches are off; warn/shadow observes without changing args.
On validates references before routing or spawning and canonicalizes args.leg.
RL26 is admission only: all modes retain the existing planner flow and its choice.
Selecting a leg by explicit reference belongs to RL20, the override gate.
Admission refusals retain validation refusal records and local host handling.
Non-null diff_ref with implement refuses in every mode, including dry runs,
and names role review. No live switch is enabled by this change.

No external STD ACCEPT or historical live gate is claimed. Synthetic tests do
not satisfy either gate; independent acceptance remains required.
