# Review quota fallthrough

A provider-shaped HTTP 429, RESOURCE_EXHAUSTED, quota reached, or quota
refusal response makes the current review leg unusable. Reviews never wait for
quiet confirmation or a reset: the spawner reaps that attempt and launches the
next eligible leg of the same route, under the original run id. The Python
output gate also recognizes review refusals after its ordinary writer startup
window. A CLI exit code of zero does not make a quota refusal a successful review.
`no_fallthrough=true` stops with the quota reason and launches no substitute.
Cancellation and surviving child processes remain terminal.

Python base and routed dispatch preserve their existing route eligibility and
seat reservation rules. Native `pg-spawnd` advances quota attempts through its
admitted route without creating another supervisor run or releasing its admission
slot to a queued run; writer families remain excluded. Each attempt retains its
output and refusal reason. The final
`chosen_leg` and response-side `served_model` identify the substitute that served.
Claude's system-init model metadata is used as a native witness; an agy request
without response-side model metadata remains `unwitnessed`.

Under D-5924 the accepted substitute is the one review on the head, rather than
an additional seat needed to replace the quota attempt. Codex writers pass
`writer_families: ["openai"]` when requesting reviews.
