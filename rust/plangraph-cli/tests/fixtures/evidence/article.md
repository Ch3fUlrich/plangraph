---
id: EV-0001
title: "Fixture: a shorter queue"
date: 2026-01-01
status: accepted
evidence_class: prospective
components: [queue]
superseded_by: null
results:
  - metric: wait
    unit: seconds
    baseline: {value: 10, window: "before", n: 2}
    after: {value: 8, window: "after", n: 2}
    effect: {absolute: -2, relative: "-20 %"}
    certainty: low
    claim_id: C01
  - metric: retries
    baseline: null
    baseline_reason: Retry counts were unavailable before the change.
    effect: null
    certainty: very_low
    claim_id: C02
---
Wait fell from 10 to 8 seconds and there were 2 retries [C01, C02].
