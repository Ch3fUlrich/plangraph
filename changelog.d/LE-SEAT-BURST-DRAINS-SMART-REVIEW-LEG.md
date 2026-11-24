Review seats on one leg now start through an admission queue. `route_policy.json` gains
`review_seat_admission` (hydrogen: 3 concurrent seats, 900 s queue wait, 15 s poll); the review route carries it
as `seat_admission` and the native spawner (`seat_admission.rs`) counts live review runs and fresh slot
reservations per leg under a lock, queues a seat beyond the bound and routes again each poll, so a leg that booked
a 429 leaves the route at once. Past the wait bound the seat moves to the next routed leg with room or is refused as
`seat_queue_timeout`, and the caller starts its fallback seats. Fifteen seats asked at once start three.
The plan-order `bin/review.sh` is not in this repository: it keeps calling spawn per seat and treats
`seat_queue_timeout` as the signal to start the two weak seats.
