# Implement route defaults (D-5206)

An implement card uses `cx-gpt-6.1-sol-xhigh` (model `omniroute/cx-gpt-6.1-sol-xhigh`,
effort `xhigh`), then `agy-gemini-3.8-flash` (agy harness). Quota, cooldown, or a
confirmed availability refusal advances without an override; safety refusals,
cancellation, and guards still stop dispatch. Eligibility and harness policy apply first.

Plain Sol and other Sol effort variants remain active, explicit-only catalog legs
requiring audited overrides. Claude/Fable/Opus/Sonnet/Haiku never enter automatic
implement routes or fallbacks; explicit picks are unchanged. No legs are deleted.
Automatic implement eligibility uses the resolver's canonical family detection for
both the catalog family and model identity, including Fable aliases even when
the legacy `claude_family` flag is absent. All catalog rows are regression-tested.

Route JSON and saved `chosen_leg` include `effort` (`xhigh` by default, otherwise
`null`). The per-run renderer declares cx effort models; see [SPAWNER.md](SPAWNER.md#card-driven-routing-core).
