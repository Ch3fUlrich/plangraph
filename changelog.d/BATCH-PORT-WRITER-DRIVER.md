The native-codex worker runs the batch writer loop: fast gates with a per-head stamp
(the commit-trailer gate included), a review run started through the spawner on the
exact head with the verdict read from that run's record, same-session fixes on
REQUEST_CHANGES up to `round_progress::MAX_ROUNDS` under the round progress rule, and one push of the
accepted head to the card's `push_ref`. Writers no longer store their own review files.
