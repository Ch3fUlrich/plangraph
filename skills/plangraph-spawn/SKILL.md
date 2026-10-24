---
name: plangraph-spawn
description: Dispatch per-host agents using plangraph cards.
---

Spawn by card only, never hand-pick a model.
Use `python -m plangraph.spawner.cli route --card @card.json --role implement`.
Then use `spawn --card @card.json --role implement "task prompt"`.
For reviews use `--role review --review-of RUN_ID`; the private writer record
is the source of the family fence. Unknown runs must never run unfenced.
Only an actual provider transport response can mark a leg limited.
An exceptional hand pick needs an audited override reason of at least ten characters.
