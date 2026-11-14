Added `merge_batch.py go-req --name NAME` to print a batch GO request from saved
part reviews, CI run evidence, and the batch PLAN key. The new `review` verb binds
ACCEPT evidence to each part head; `push --ci-run ID` records batch CI. Requests
refuse missing or stale reviews, non-green CI, and workflow edits without a
Gate-Edit trailer on the touching commit, using the existing gate checker.
