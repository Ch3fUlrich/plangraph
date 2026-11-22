Fixed the D-6517 findings of the Temporal pilot worker: a typed admission refusal ends the card before any
attempt report or probe, the probe entry applies the admission policy and runs the card's check inside the
attempt jail (failing closed without it), supervised output is redacted before the capture limit drops its
front (a secret on the cut leaves no fragment), activity failure messages are redacted and capped before they
reach Temporal, and the pilot payload test asserts the report and park packet the workflow builds.
