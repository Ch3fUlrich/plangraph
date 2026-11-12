Seat verdict backfill: extract batch orchestration and shared-outbox delivery into
`tools/seat_backfill.py` so the record and verdict modules stay below 400 lines.
Existing public imports, CLI behaviour, record identity bindings, and the sole
`seat_verdict.main()` posting path are unchanged.
