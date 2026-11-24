# Review seat admission per leg

Review seat admission per leg: `route_policy.json` `review_seat_admission` (`legs`: leg id to maximum concurrent
seats, `queue_wait_seconds`, `poll_seconds`) rides on the review route as `seat_admission`. The native spawner
counts live review runs plus fresh slot reservations (`<state>/seat-slots/`) per leg, queues a seat beyond the bound,
routes again each poll (a leg that booked a 429 leaves the route at once), and past the wait bound moves to the next
routed leg with room or refuses `seat_queue_timeout` so the caller starts its fallback seats.
