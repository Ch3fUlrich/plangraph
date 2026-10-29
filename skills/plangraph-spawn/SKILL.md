---
name: plangraph-spawn
description: Dispatch per-host agents using plangraph cards.
---

Spawn by card via plangraph-spawn (`python3 tools/plangraph-spawn.py ... spawn --card ...`), never hand-pick a model.
Use `python3 -m plangraph.spawner.cli route --card @card.json --role implement`.
Then use `spawn --card @card.json --role implement "task prompt"`.
Use the returned `plangraph-spawn wait RUN_ID --timeout SECONDS` to await completion.
Never use `pgrep -f` wait loops: they can match the waiting process itself.
For reviews use `--role review --review-of RUN_ID`; the private writer record
is the source of the family fence. Unknown runs must never run unfenced.
Only an actual provider transport response can mark a leg limited.
An exceptional hand pick needs an audited override reason of at least ten characters.
