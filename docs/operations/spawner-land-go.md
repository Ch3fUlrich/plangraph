# Land a batch on a GO row

`pg-spawnd land-go --rows FILE --done FILE --repo OWNER/NAME --checkout DIR --post-cmd CMD
[--keeper-cmd CMD]` is one pass over the router's GO rows; a timer runs it, and the trigger is a
GO row for an exact batch head. A row is one line with `GO`, `D-<id>`, the batch pull request
(`#<n>`), the full head and optional `plangraph:<KEY>` item keys. For each row not yet in the
done file the pass checks that the head GitHub shows is the row's head, that it is 0 commits behind
main and that the required checks are green, then runs `tools/merge_batch.py land` with the row as
approval, posts `MERGED <sha> #<n> <head> <D-id>` through the post command and runs `close-merged
KEY D-<id>` as a dry run per key, posting its result or refusal. A moved head, a stale
base, a red check or a refused land posts `BLOCKED land <D-id> #<n> <head> | <reason>` and lands
nothing; each `<D-id> <head>` is handled once. Any other trigger does not land.
