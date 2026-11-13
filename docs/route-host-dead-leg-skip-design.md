# Host dead legs before spawn

Plan: `plangraph:ROUTE-HOST-DEAD-LEG-SKIP-BEFORE-SPAWN`; replan-0449.
D-5714, D-5722 and rule 1h. Round one is design and recorded evidence only.
No runtime fix or host configuration change is claimed here; the design-doc search found `docs/rl29-reconcile-design.md` beside this file.

## Current code map

- Route entry: `src/plangraph/spawner/service.py:SpawnService._route`.
  It calls `src/plangraph/routing_support/leg_state_flag.py:service_plan`.
  `src/plangraph/planner/routing.py:plan_spawn` builds legs, skips and seats.
  `src/plangraph/planner/routing.py:route_card` applies admission and review order.
  `src/plangraph/planner/review_order.py:review_key` supplies the ordering key.
- Host admission: `src/plangraph/routing_support/resolver.py:host_skip_reason`.
  `served_models` discovers standalone models; `plan_for_launch` writes host skips.
  `src/plangraph/routing_support/served_fresh.py:served_fresh` confirms absence.
  `src/plangraph/routing_support/leg_state.py:LegAdapter.leg_status` joins discovery and provider state.
  Native harness admission currently checks the executable, not model selection.
- Bookings: `src/plangraph/routing_support/leg_state.py:LegStateStore._connect` creates `leg_events`.
  `_default_store` selects `$XDG_STATE_HOME/plangraph/leg-state.sqlite`.
  `record_provider_response` and `record_harness_limit` write `reset_at`, `ts` and scope.
  `_reset` caps resets at 24 hours. `leg_status` expires evidence after six hours.
  `src/plangraph/spawner/exit_outcome.py:_book_failure` writes harness bookings.
  `src/plangraph/spawner/runner.py:SpawnerRunner.provider_response` writes trusted HTTP evidence.
  `src/plangraph/spawner/leg_probe.py:before_launch` checks bookings before probes.
- Plan audit: `src/plangraph/planner/routing.py:plan_spawn` fills `SpawnPlan.skipped`.
  `src/plangraph/planner/routing.py:SpawnPlan.to_dict` serializes that audit.
  `src/plangraph/routing_support/resolver.py:plan_for_launch` appends `reason` and `skip_reason`.
- Attempts: `src/plangraph/spawner/service.py:SpawnService._spawn.execute` appends actual results.
  `src/plangraph/spawner/runner.py:spawn_service.DispatchService._spawn.execute` also appends pre-launch skips.
  `src/plangraph/spawner/service.py:SpawnService._save` synthesizes unlaunched rows at run end.
  Both synthesis paths must stop doing that for this design's skips.

## (a) Host and deadline admission

Filter on the executing host, after placement, before prompt packaging or child spawn.
Apply the same filter to routed reviews, seat plans and explicit selections.
An explicit selection refuses if blocked. It cannot override a booking.
Offline route and dry-run calls do not probe or write host health.
Use one run deadline: `record.started + thresholds(card, role='review')['max_s']`.
After PR 808, reuse `run_watchdog_limits.py:leg_limits` (re-exported by run_watchdog) and its unchanged deadline; max_s=None has no deadline-based skip, but active bookings still block launch.
Drop confirmed host absences with `not_served_on_host: model_not_listed`.
Drop a fresh active quota booking ending at or beyond the deadline with `quota_dead_past_deadline`.
Put leg id, reason, skip_reason and bounded evidence in `plan.skipped`; keep reset_at and deadline numeric, without provider text.
Write no attempt row for these decisions, including during final save or reroute.
Only a real launch gets an attempt, including the launch that discovers failure. Recheck before each launch; deduplicate skips by leg and reason.
Filter review seats too, then recompute family coverage using existing seat rules.
Do not change the relative order of survivors. No survivors means a recorded refusal or exhaustion, not a dummy attempt.

D-3685 bookings are never overridden by a probe, snapshot or serving-log hint.
Read active bookings independently of the newest probe event, with exact account/combo scope.
Keep the existing leg-wide harness scope and `agy/` key normalization.
Add `LegStateStore.active_booking`; do not trust a latest-event `ok` as booking clearance.
Today `leg_state.py:235-241` can return ok after a later probe pass.
The new admission query must still find the older unexpired booking.
Use the capped reset and six-hour evidence expiry; do not park a leg for the reported 146 hours.
At expiry require fresh authority. Do not silently extend evidence or renew it by probing.
A reset before the deadline is not quota-dead for the whole run.
While it remains booked, use `quota_limited_until_reset` in the plan and launch nothing.
Reconsider it on an existing reroute after reset; do not add sleeping or polling.
An absent reset uses the existing ten-minute booking window, never an invented long reset.

## (b) Bounded gateway refusal

Only trusted ECONNREFUSED evidence creates a mark: `resolver.py:gateway_reachable` makes a parent-side connection check bounded to one second and remaining run time; `SpawnerRunner.gateway_transport_response` also accepts authenticated adapter telemetry. Timeout or DNS failure stays unknown.
Exit code 1, prompt text and model-authored output alone are insufficient.
Add a private `host_gateway_health` table to the existing leg-state SQLite database.
Key it by canonical executing host and a digest of the effective gateway configuration.
Store reason `gateway_connection_refused`, observed_at and expires_at; no endpoint or key.
The mark lasts 300 seconds from the observation. Reads and skipped legs never renew it.
All legs using that same gateway get `not_served_on_host: gateway_connection_refused`.
Direct/native legs and other hosts or gateway digests are unaffected.
Expiry or a changed configuration makes the mark inapplicable.
A newer authenticated transport success clears it early; cached discovery does not.
Unreadable health storage is unknown, not proof of absence. Surface a bounded diagnostic.

## (c) MiMo on dell

`src/plangraph/routing_support/opencode_overlay.py:overlay_env` reads `$AUTOOS_ROOT/opencode.jsonc`.
`src/plangraph/routing_support/opencode_render.py:render_gateway_providers` renders `OPENCODE_CONFIG_CONTENT`.
Address precedence is `providers.omniroute.settings.baseURL`, `OMNIROUTE_BASE_URL`,
`PLANGRAPH_GATEWAY_URL`, then transitional `AUTOOS_OMNIROUTE_URL`; a valid provider baseURL is the final fallback.
`src/plangraph/routing_support/host_keys.py`
function `add_host_keys` supplies the named gateway credential privately.
dell needs an authenticated gateway reachable from its child network, not merely valid rendered config.
No address or credential value belongs in this repository. This round changes none.
Without that connectivity, dell's host health marks both gateway MiMo legs not served for the bounded window.
`resolver.py:admitted_by_render` currently bypasses model listing for MiMo; it must not bypass host health.

## (d) Served legs stay eligible

A leg needs valid registration, the requested role, policy/risk/context admission and its host executable.
It needs confirmed model support or the accepted rendered-provider path, and no active quota or gateway mark.
Such a leg is never skipped by this filter, including when snapshot telemetry is missing or stale.
A failed discovery is unknown, not an empty served list. Preserve PR 812's recovery distinction.
Do not turn snapshot_error, generic exit 1 or an invalid-model error into a fleet-wide quota booking.
Confirmed native invalid-model selection is host/model absence, not provider exhaustion; use a separate `host_model_health` table keyed by host/model/config digest, expiring after 300 seconds or config change.

## (e)-(f) Test-first slices and merge ownership

Each later slice includes its tests and docs and stays at most 400 changed lines.
Keep every existing test name and assertion. Add separate booking-admission tests.
Round one adds only `test_route_host_dead_leg_attempts_fixture_shape` and the fixture below.
1. Booking authority, <=350 lines: add `leg_state.py:LegStateStore.active_booking`; use it in `leg_probe.py:before_launch` and `routing.py:plan_spawn`.
   Add `tests/test_route_host_booking.py`: `test_probe_pass_cannot_override_booking`, `test_quota_deadline_boundary`, `test_quota_scope_and_expiry`, `test_booked_leg_never_probed`.
   Wait for PR 812 before editing its `before_launch` admission hunk. Keep snapshot retry and scrub handling.
2. Host filter, <=350 lines: change `resolver.py:plan_for_launch`, `host_skip_reason`, `host_skip_reason_async`; thread host/deadline through existing async wrappers.
   Add `tests/test_route_host_admission.py`: `test_not_served_removed_before_spawn`, `test_served_leg_never_skipped`, `test_discovery_failure_is_unknown`, `test_native_invalid_model_is_host_scoped`.
   Wait for PR 812's `LegAdapter.leg_status` merge before consuming snapshot states. Wait for PR 813's resolver/MiMo changes; retain its new identities.
3. Gateway health, <=400 lines: add `leg_state.py:LegStateStore.record_gateway_refusal`, `gateway_status`, `clear_gateway_refusal` and table setup in `_connect`; consult health in `resolver.py:host_skip_reason`.
   Change `exit_outcome.py:fallthrough_reason`, `runner.py:SpawnerRunner.spawn`; add `resolver.py:gateway_reachable` and private-token-authenticated `SpawnerRunner.gateway_transport_response`, not output prose parsing. Native health belongs in slice 2: `LegStateStore.record_host_model_absence`, `host_model_status`.
   Add `tests/test_route_host_gateway.py`: `test_gateway_refusal_shared_only_on_host`, `test_gateway_mark_expires_without_renewal`, `test_gateway_success_and_config_change`, `test_exit_one_does_not_mark_gateway`, `test_mimo_render_cannot_bypass_health`.
   Wait for PR 808's failure-classification hunk. Keep its terminal safety stops and ordinary-failure fallthrough.
4. Dispatch/audit, <=400 lines: change `service.py:SpawnService._route`, `_spawn`, `_save`; change `runner.py:spawn_service.DispatchService._spawn` and `seat_spawn.py:SeatSpawnMixin._spawn_seats`.
   Reuse existing functions; shrink/extract existing responsibilities if a file would exceed its file-size cap.
   Add `tests/test_route_host_dead_leg_skip.py`: `test_recorded_dead_legs_leave_no_attempts`, `test_final_save_does_not_synthesize_dead_attempts`, `test_reroute_keeps_skip_audit`, `test_no_survivors_launch_nothing`, `test_seats_recompute_coverage`, `test_explicit_leg_cannot_bypass_host_filter`.
   Wait for PR 808's attempt-loop and remaining-deadline hunks in service.py and runner.py; add admission before its launch, not a competing loop.
   Wait for PR 813's `planner/legs.py`, `routing.py:plan_spawn` order/benchmark hunks; test survivor order without restoring old Argon review roles.

## Recorded evidence boundary

`tests/fixtures/route_host_dead_leg_attempts.json` projects all 26 attempt rows from each of the five requested runs.
It preserves order and only leg id, status, skip_reason and reason. Missing reason fields become null.
Four runs fail on Argon; the fifth already skips it as limited. All five eventually complete on Haiku.
The supplied evidence used PR 808. Main stops on an ordinary failed leg without its fallthrough change.
The fixture's MiMo rows say only harness exit code 1. They do not prove ECONNREFUSED or a gateway address.
Connection refusal and the roughly 146-hour agy quota are supplied context, not invented fixture fields.
Future tests inject trusted transport and quota evidence separately. This round's shape test asserts no runtime fix.
