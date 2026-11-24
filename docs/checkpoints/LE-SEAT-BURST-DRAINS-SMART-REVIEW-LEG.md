# Review seat admission per leg

Review seat admission per leg: `route_policy.json` `review_seat_admission` (`legs`: leg id to maximum concurrent
seats, `queue_wait_seconds`, `poll_seconds`) rides on the review route as `seat_admission`. The native spawner
counts live review runs plus fresh slot reservations (`<state>/seat-slots/`) per leg, queues a seat beyond the bound,
routes again each poll (a leg that booked a 429 leaves the route at once). It never falls through to another
leg of the queued leg's harness: past the wait bound it refuses `seat_queue_timeout`, and a route that replaces the
leg with a sibling of the same harness (the `agy-cli` selector keeps only agy legs) refuses `seat_leg_limited`, so the
caller starts its fallback seats. A route that moves to another harness is that fallback and is taken.
