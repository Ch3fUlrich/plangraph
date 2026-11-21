A re-review of the same head (the previous review's head equals the head) now sends the
full batch base..head diff: the native writer review card falls back to the batch base and
its prompt carries the previous review as context. The spawner checks a review card's
diff once before any leg and fails the card with `empty_diff` instead of skipping every
inline leg with `inline_diff_failed: empty diff`. The plan-order `bin/review.sh` is not in
this repository: it must stop setting diff_ref base to REVIEW_PREV's head when that head is
the head under review, and may rely on the spawner's `empty_diff` card error.
