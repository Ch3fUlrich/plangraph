# Review seat admission per leg

Review seat admission per leg: `route_policy.json` `review_seat_admission` (`legs`: leg id to maximum concurrent
seats, `queue_wait_seconds`, `poll_seconds`) rides on the review route as `seat_admission`. The native spawner
counts live review runs plus fresh slot reservations (`<state>/seat-slots/`) per leg, queues a seat beyond the bound,
routes again each poll (a leg that booked a 429 leaves the route at once). It never falls through to another
leg of the queued leg's harness: past the wait bound it refuses `seat_queue_timeout`, and a route that replaces the
leg with a sibling of the same harness (the `agy-cli` selector keeps only agy legs) refuses `seat_leg_limited`, so the
caller starts its fallback seats. A route that moves to another harness is that fallback and is taken.

A seat is matched to its slot by name: the slot's token rides on the request, the run record stores it as
`seat_reservation`, and a record naming the token takes the slot over (a start time never matches). A
request cannot claim a token. A review split into groups admits each group under its own slot (the first group
keeps the review's), so a split larger than the bound waits like any other burst. When admission refuses with
`seat_queue_timeout` or `seat_leg_limited` (also when the `agy-cli` selector leaves no agy leg), the spawner
starts both fallback seats itself (`opencode-go-deepseek-v4-flash` and `opencode-go-glm-5.3-flash`, from the
route without the selector) and answers with their seat group; if either leg is not routable none starts
(`seat_fallback_unavailable`).

The wait budget is a monotonic deadline: a queued seat that has used its budget is refused even when a slot
just freed, and the last poll sleeps only the time left. A quota retry into a later leg takes a slot there
first (a full leg is skipped like any unavailable leg) and the run counts on the leg of its running attempt.
A split group that waits routes again each poll and is refused `seat_leg_limited` when its leg left the
route; the groups already started stay in a failed seat group that keeps their run ids. A fallback seat that
itself split answers with every run it started, and a second seat that fails to launch leaves the first in
a failed seat group.
