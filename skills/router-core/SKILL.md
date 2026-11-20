---
name: router-core
description: How the portfolio router works. Use at router start and before any router step that touches more than one item — a keeper plan batch, the GO for a batch PR, grants for several lanes, or a group of asks to plan-order. Holds the batch-mode procedure (rule 3.58 (3)).
---

# Router core

The router decides, records and relays; it applies keeper writes only as the router principal
(`apply_ops` is router-only, `docs/KEEPER.md`). Every successor and every project that does its
own routing loads this skill at start: the router launch prompt names it.

## Batch mode

Work in batches whenever more than one item is involved. One command each, one D-id each, one
reply that lists what was applied and what was refused.

1. **Keeper plan batch.** `python3 tools/apply_batch.py BATCH.yaml --dry-run` checks every
   entry and lists every refused entry (`op N name key: error ...`), then states
   `refused K of M entries; nothing applied`. `--dry-run --preview` asks the keeper itself. The live run is
   atomic (one `apply_ops`): one refused entry means nothing applied, and the reply names the
   entry. Run it as the router principal with one D-id for the whole file; reply with the
   applied and refused entries, never "refused" alone. Fix the file and run it again.
   Do not split a batch to get around one entry.
2. **GO for a batch PR.** Write the input (D-id, PR number, batch head, one part per writer
   branch: name, head, CI state, review file, writer family, reviewer family) and run
   `plangraph router go-row INPUT.json`. It refuses, naming the part, when a head is not 40
   hex, CI is not green, a review file or family is missing, or the reviewer family is the
   writer's. It prints the one GO row with the per-part evidence table; add that row to the
   decisions file in one commit.
3. **Grants for several lanes.** `plangraph router grant-rows INPUT.json OUT` writes one line per
   grant (lane, grantee, scope) under one D-id into one row file; the grants land in one
   decision PR. A lane listed twice refuses the file.
4. **Asks to plan-order.** Related asks go as one group: `plangraph router ask-group INPUT.json`
   prints one numbered message (`ASK-GROUP <to> | <topic> | N item(s)`, one line per item).
   Send it as one message, not N.

Single items stay single: this section is for two or more. Read each command's refusal as the
answer; never edit its output to pass.
