Review seats on one leg now start through an admission queue. `route_policy.json` gains
`review_seat_admission` (hydrogen: 3 concurrent seats, 900 s queue wait, 15 s poll); the review route carries it
as `seat_admission` and the native spawner (`seat_admission.rs`) counts live review runs and fresh slot
reservations per leg under a lock, queues a seat beyond the bound and routes again each poll, so a leg that booked
a 429 leaves the route at once. Past the wait bound the seat is refused as `seat_queue_timeout`, and a route that swaps the
leg for a sibling of the same harness is refused as `seat_leg_limited`; the spawner then starts the two fallback seats. Fifteen seats asked at once start three.
A seat's slot is matched to its run record by a token, each group of a split review takes its own slot, and
a refused seat starts the DeepSeek and GLM fallback seats in the spawner and answers with their seat group.
