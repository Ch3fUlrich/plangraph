Add the parallel-trains driver steps: `land-train plan` decides from the trains' CI state and the live
tip of main (land the first green train, remerge every other green or running train, drop the
attributable part of a red one) and `land-train remerge` merges the new main into an open train,
verifies it and moves its branch without any new part review. The GO verifier still expects the
manifest base to equal main's tip; that gate-path change is a separate item.
