# Implement route defaults (D-5206)

An implement card uses `cx-gpt-6.1-sol-xhigh` (model `omniroute/cx-gpt-6.1-sol-xhigh`,
effort `xhigh`), then `agy-gemini-3.8-flash` (agy harness). Quota, cooldown, or a
confirmed availability refusal advances without an override; safety refusals,
cancellation, and guards still stop dispatch. Eligibility and harness policy apply first.
Both designated writers precede all other cost tiers, even when catalog tiers
change: Sol xhigh, then Gemini Flash (rule 3.23a / D-5212). Other eligible legs
retain cost-tier order, with subscriptions before pay-as-you-go.

Plain Sol and other Sol effort variants remain active, explicit-only catalog legs
requiring audited overrides. Claude/Fable/Opus/Sonnet/Haiku never enter automatic
implement routes or fallbacks; explicit picks are unchanged. No legs are deleted.
Automatic implement eligibility wraps canonical family detection with implement-only
Anthropic/Fable aliases for the leg id, family, model and harness, including effort-bearing aliases even
when `claude_family` is false or the model is renamed. Unidentified families fail
closed; a false legacy flag never overrides Claude identity. All catalog rows are
regression-tested. Shared scoring, review and writer-family fences retain their
existing resolver behavior. Route JSON exposes `excluded_family` in skipped and
candidate rows; CLI stderr names each skipped leg, including when no route remains.

Route JSON and saved `chosen_leg` include `effort` (`xhigh` by default).
Operator leg JSON preserves its validated `effort` through the loaded leg, route
plan, saved `chosen_leg`, and attempt rows; omitted or explicit `null` effort
stays `null`. All other validated leg fields are retained too. The per-run
renderer declares cx effort models; see [SPAWNER.md](SPAWNER.md#card-driven-routing-core).
