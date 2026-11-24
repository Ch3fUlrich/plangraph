Add the parallel-trains driver steps: `land-train plan` decides from the trains' CI state and the live
tip of main (land the first green train, remerge every other green or running train, drop the
attributable part of a red one) and `land-train remerge` merges the new main into an open train,
verifies it and moves its branch without any new part review. The GO verifier still expects the
manifest base to equal main's tip; that gate-path change is a separate item.
`land-train drive` runs the steps through the production builder; the push gate replays a remerge
and refuses one that dropped a landed change; the branch moves by compare-and-swap with its manifest
staged first; remerge takes the resolution log and protected paths of `verify`.
Round 2: the push check resolves a relative `--repo` before cloning (the hook passes `.`), refuses a
train holding main history that no replayed remerge accounts for, plans a landing alone, keeps every
candidate of an unattributed red train and retries a publish that failed after the branch moved.
`Delta::replay` is the replay record the GO evidence is built from.
