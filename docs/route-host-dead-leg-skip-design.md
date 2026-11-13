# Host dead legs before spawn

Plan: `plangraph:ROUTE-HOST-DEAD-LEG-SKIP-BEFORE-SPAWN`; replan-0449.
D-5714, D-5722, D-5736 and rule 1h. Round two is design and recorded fixtures only.
No runtime fix or host configuration change is claimed. No lane change belongs under `src/`.
Ignore `docs/rl29-reconcile-design.md` for this item; it governs crash reconciliation, not routing.
It is neither superseded nor merged into this contract.

## Current code map

- Route: `src/plangraph/spawner/service.py:SpawnService._route` calls
  `src/plangraph/routing_support/leg_state_flag.py:service_plan`.
  `src/plangraph/planner/routing.py:plan_spawn` builds legs, skips and seats.
  `routing.py:route_card` and `src/plangraph/planner/review_order.py:review_key` order reviews.
- Host: `src/plangraph/routing_support/resolver.py:host_skip_reason`, `plan_for_launch`,
  `served_models` and `src/plangraph/routing_support/served_fresh.py:served_fresh` handle discovery.
  `src/plangraph/routing_support/leg_state.py:LegAdapter.leg_status` joins snapshot and provider state.
  Native admission currently checks the executable, not model selection.
- Bookings: `leg_state.py:LegStateStore._connect` creates `leg_events`.
  `_default_store` selects `$XDG_STATE_HOME/plangraph/leg-state.sqlite`.
  `record_provider_response`, `record_harness_limit` and `_reset` store capped resets.
  `leg_status` currently expires evidence at six hours and can accept a newer probe pass.
  `src/plangraph/spawner/leg_probe.py:before_launch` checks bookings before probes.
  `src/plangraph/spawner/exit_outcome.py:_book_failure` books harness limits.
  `src/plangraph/spawner/runner.py:SpawnerRunner.provider_response` accepts trusted HTTP evidence.
- Audit: `routing.py:SpawnPlan.to_dict` serializes `plan_spawn` skips.
  `resolver.py:plan_for_launch` appends canonical `reason` and `skip_reason`.
- Attempts: `service.py:SpawnService._spawn.execute` appends results.
  `runner.py:spawn_service.DispatchService._spawn.execute` also appends pre-launch skips.
  `service.py:SpawnService._save` synthesizes unlaunched rows. Both synthesis paths must change.

## (a) Host, booking and deadline admission

Filter per leg on the executing host after placement, before prompt packaging and child spawn.
Use the same admission for routed reviews, seats and explicit selections; pins cannot override it.
Offline route and dry-run calls neither probe nor write health.
Resolve the deadline once at run creation: before PR 808 use
`record.started + run_watchdog_limits.thresholds(card, role=role)['max_s']`;
after PR 808 use its `leg_limits` deadline unchanged. Never recompute it at reroute.
An unlimited `max_s=None` disables only the deadline comparison, not booking admission.
Confirmed absence gets `not_served_on_host: model_not_listed`.
An active booking ending at or beyond a finite deadline gets `quota_dead_past_deadline`.
An active booking ending earlier, or with no run deadline, gets `quota_limited_until_reset`.
Both reasons block only that booked leg and scope, never other eligible legs or the whole run.
Bookings take precedence over served status, rendered admission and all probe results (D-3685).
Add `LegStateStore.active_booking`; query unexpired limits independently of newest-event status.
Keep exact provider account/combo scope, existing leg-wide harness scope and `agy/` normalization.
An explicit reset books until `min(provider_reset, observed_at + 24 h)`, not six hours.
Persist whether a reset was capped/absent in `leg_events`; migrate old uncertain rows as requiring revalidation.
The six-hour evidence TTL never clears an active booking; change booking readers accordingly.
An absent reset books ten minutes; an explicitly elapsed reset books nothing.
A genuine reset releases its booking at reset. A truncated or absent reset needs revalidation at expiry.
Revalidation means a trusted adapter quota-status response or operator-authorized clearance
for the same scope, observed after expiry; a liveness probe, snapshot or serving hint cannot clear it.
Until revalidated, an expired uncertain booking becomes `leg_state_snapshot_error`, not launchable.
This ends the capped booking without inventing a 146-hour booking or spending a routing attempt.
Never probe an active booking, renew it by reads, sleep, or poll for reset.
Reconsider a genuinely reset or revalidated leg only at an existing reroute/admission boundary.
Store leg id, canonical reason, skip_reason, numeric reset_at/deadline and bounded evidence in `plan.skipped`.
No pre-launch rejection creates an attempt, including final save, reroute or seat synthesis.
Only a real launch creates an attempt, including a new failure discovered after admission.
Recheck before every launch; deduplicate skips by leg and reason.
Preserve survivor order and recompute seat-family coverage with existing rules.
No survivors means recorded refusal/exhaustion, never a dummy attempt.

## (b) Gateway evidence in the launching process

A gateway-backed leg with no valid effective base URL is not served on that executing host.
Use `not_served_on_host: gateway_base_url_unresolved`; do not turn missing configuration into unknown.
Resolve and check inside `SpawnerRunner.spawn`, in the process that launches the child.
Use its final child environment/config after overlays, key loading, rendering and isolation.
Freeze that launch configuration; a changed environment/config requires admission again.
Never substitute an interactive shell, remote parent, or differently configured discovery process.
Add `resolver.py:gateway_reachable(env, deadline)` for this launcher-side check, not an ambient-env check.
It resolves the rendered effective URL first and returns unresolved before any connection if absent.
Connect with the child's proxy/network settings, bounded by one second and remaining run time.
Only the connector's OS `ECONNREFUSED` yields `not_served_on_host: gateway_connection_refused`.
Timeout, DNS failure or unverifiable network-context parity is unknown; skip with `gateway_check_unknown`.
Define `SpawnerRunner.gateway_transport_response` as an in-process connector callback only.
Bind it to host/config digest and a private object capability, following `provider_response` token checks.
Never export that capability to a child. Exit-one output never calls this callback.
No child prose parsing or unspecified authenticated telemetry is required to learn refusal.
Add private `host_gateway_health` in the existing leg-state database, keyed by host/config digest.
Store reason, observed_at and expires_at, never an endpoint, key or environment value.
A refusal mark blocks every leg using that gateway on that host for 300 seconds.
Direct/native legs and other hosts/configurations are unaffected; skipped legs never renew a mark.
Expiry requires a new bounded connector check before launch, not a paid child attempt.
A configuration change invalidates the old mark but still requires admission.
A newer successful connector check clears refusal; discovery alone cannot clear it.
Missing URLs are checked on every admission; a 300-second expiry never makes them launchable.
Unreadable health storage is unknown and fail-closed under (d), with a bounded diagnostic.

## (c) MiMo on dell: D-5736

D-5736 establishes that dell's spawner lacked the gateway base-URL environment variable.
This is a launcher configuration failure, not evidence that a correctly configured gateway was unreachable.
`src/plangraph/routing_support/opencode_overlay.py:overlay_env` reads `$AUTOOS_ROOT/opencode.jsonc`.
`src/plangraph/routing_support/opencode_render.py:render_gateway_providers` renders child configuration.
Keep its address precedence: `providers.omniroute.settings.baseURL`, `OMNIROUTE_BASE_URL`,
`PLANGRAPH_GATEWAY_URL`, transitional `AUTOOS_OMNIROUTE_URL`, then a valid active-provider baseURL.
`src/plangraph/routing_support/host_keys.py` function `add_host_keys`
privately supplies the named gateway credential.
The environment variable must reach the executing spawner, not just its shell or controller.
For the recurrence with no effective URL, list both gateway MiMo legs not served with `gateway_base_url_unresolved`.
If the resolved gateway refuses, list them not served with `gateway_connection_refused` under (b).
`resolver.py:admitted_by_render` must not bypass either rule or fail-closed unknown admission.
After configuration repair, MiMo still needs successful launch-context admission before answering.
This round changes no host setting and records no address, credential or site string.

## (d) Eligibility and unknown state

A served leg stays eligible only with registration, role, policy/risk/context approval, executable,
confirmed model support or valid rendered admission, and no blocking booking or host-health decision.
A leg meeting all those conditions is never skipped, even when unrelated snapshot telemetry is absent.
Missing/stale telemetry may be replaced by independent authoritative evidence for every required check.
Otherwise an unknown leg is skipped with `leg_state_snapshot_error`; it is never optimistically launched.
Discovery failure is unknown, not an empty served list or a permanent host absence.
Preserve PR 812 snapshot recovery/scrub semantics; no unknown state creates a fleet-wide quota booking.
Confirmed native invalid-model selection creates host/model absence, not provider exhaustion.
Add `host_model_health` keyed by host/model/config digest, including executable/model-settings identity.
Use `not_served_on_host: native_model_invalid` until configuration changes or an authoritative model-list check clears it.
Do not expire configuration-caused invalid-model marks after 300 seconds; elapsed time cannot permit another failed attempt.
Generic exit 1 alone proves neither gateway refusal nor native model absence.

## (e) Test-first slices

Budgets include tests/docs and all listed functions. Measure added + deleted lines against each slice base.
Split an over-budget slice before commit; do not omit native-health work to claim the 350-line budget.
Retain all existing test names/assertions. This round tests evidence, not runtime admission.
1. Booking authority, <=350 lines: `leg_state.py:LegStateStore.active_booking`, `_connect`, `_reset`,
   `record_provider_response`, `record_harness_limit`, `leg_status`; `leg_probe.py:before_launch`, `routing.py:plan_spawn`.
   `tests/test_route_host_booking.py`: `test_probe_pass_cannot_override_booking`, `test_quota_deadline_boundary`,
   `test_quota_scope_and_expiry`, `test_booked_leg_never_probed`, `test_six_hour_ttl_cannot_clear_booking`,
   `test_capped_reset_requires_authoritative_revalidation`, `test_booking_blocks_only_its_leg`.
   First red-on-main test: `test_probe_pass_cannot_override_booking`; newer probe passes currently hide bookings.
2. Host filter/native health, <=350 lines: `resolver.py:plan_for_launch`, `host_skip_reason`,
   `host_skip_reason_async` and existing async wrappers; `leg_state.py:LegStateStore._connect`,
   `record_host_model_absence`, `host_model_status` own the native table and configuration-scoped clearing.
   `tests/test_route_host_admission.py`: `test_not_served_removed_before_spawn`, `test_served_leg_never_skipped`,
   `test_discovery_failure_is_unknown`, `test_unknown_leg_is_skipped_without_attempt`,
   `test_native_invalid_model_is_host_scoped`, `test_native_invalid_model_does_not_expire_by_time`.
3. Gateway health, <=400 lines: `leg_state.py:LegStateStore._connect`, `record_gateway_refusal`,
   `gateway_status`, `clear_gateway_refusal`; `resolver.py:host_skip_reason`, `gateway_reachable`;
   `runner.py:SpawnerRunner.spawn`, `gateway_transport_response`; `exit_outcome.py:fallthrough_reason`.
   `tests/test_route_host_gateway.py`: `test_gateway_refusal_shared_only_on_host`,
   `test_gateway_mark_expires_without_renewal`, `test_gateway_success_and_config_change`,
   `test_exit_one_does_not_mark_gateway`, `test_mimo_render_cannot_bypass_health`,
   `test_missing_gateway_url_in_spawner_env_skips_before_spawn`, `test_parent_env_cannot_admit_child`.
4. Dispatch/audit and live verification, <=400 lines: `service.py:SpawnService._route`, `_spawn`, `_save`;
   `runner.py:spawn_service.DispatchService._spawn`; `seat_spawn.py:SeatSpawnMixin._spawn_seats`.
   `tests/test_route_host_dead_leg_skip.py`: `test_recorded_dead_legs_leave_no_attempts`,
   `test_final_save_does_not_synthesize_dead_attempts`, `test_reroute_keeps_skip_audit`,
   `test_no_survivors_launch_nothing`, `test_seats_recompute_coverage`, `test_explicit_leg_cannot_bypass_host_filter`.
   The recorded replay must first fail on main's pre-launch/synthesized attempt rows and assert named plan reasons.
   Replay all five recorded plans; inject separately labelled quota/host authority, never infer it from null fields.
Reuse/extract existing responsibilities; never grow a file beyond its file-size ratchet.

## (f) Merge ownership

The original brief (`run-c76c4cd5bda7486da2942ddb93fbe5ec`) assigns (e) to functions/tests/slices and (f) to merge dependencies.
They are separate requirements; this document keeps separate sections rather than merging their meaning.
Slice 1 waits for PR 812's `before_launch` hunk; slice 2 waits for its `LegAdapter.leg_status` snapshot hunk.
Slices 2/3 wait for PR 813 resolver/MiMo identities; retain them and snapshot recovery/scrub handling.
Slices 3/4 wait for PR 808 failure classification, attempt loop and remaining-deadline hunks in service/runner.
Insert admission before that loop's launch; preserve terminal safety stops and ordinary-failure fallthrough.
Slice 4 waits for PR 813 `planner/legs.py` and `routing.py:plan_spawn` ordering/benchmark hunks.
Test survivor order without restoring old Argon roles. Merge main into the lane; never rebase.

## Recorded evidence boundary

`tests/fixtures/route_host_dead_leg_attempts.json` preserves the five runs' 26 ordered rows each.
Keep raw status/skip_reason/reason, including failure text in skip_reason and absent reason as null.
Do not relabel these historical rows as prospective named admission reasons.
The fixture test pins those failure fields; later replay tests assert canonical reasons in the new plan.
All five runs finish on Haiku with PR 808; MiMo's exit-one rows alone prove no transport cause.
The host search of `~/.local/state/plangraph/runs/*/record.json` found recorded agy 429 quota responses.
`tests/fixtures/route_host_dead_leg_quota.json` adds two agy-sonnet-5.5 responses from
`run-29c2b8e5c20c474b921d475182d22464` and `run-726ed4a8c28b4aa888d64bddf8c73c46`.
Project leg id/status/skip_reason from attempts and reason from `stderr_tail`'s structured `AGY_ERROR.short_error`.
Their code is 429 and their resets are 145h52m0s/145h52m13s; no absolute reset_at was recorded, so keep null.
Their `provider_response` fields are null; this is recorded adapter stderr, not authenticated HTTP callback evidence.
The first fixture cannot alone prove quota-dead admission. Its replay uses the supplemental recorded quota text.
Any numeric reset_at, clock, deadline and trusted booking injected for replay are constructed, not recorded facts.
Never change raw evidence to manufacture a reason or a timestamp. No prompt/stdout/endpoint/key/home path is retained.

## Verification and completion ownership

Slice 4 owns the item's live acceptance, after slices 1-3 land: one unpinned dell review must reach a serving leg
with zero failed attempts; MiMo must answer or appear as not served with its named reason. Record the run id in its PR body.
The evidence-only shape/projection tests may pass on main; they do not satisfy the item's red-first runtime test.
Each slice runs touched test files and `python3 tools/affected_tests.py --base origin/main --run -n 2`.
If selection falls back to the whole suite, use `--within` for the lane's test files; never run the whole suite.
Before every lane push run `python3 tools/check_new_code_lang.py`, `python3 tools/check_changelog_docs.py`,
`CHANGELOG_BASE=origin/main python3 tools/check_changelog_docs.py`,
`FILESIZE_REQUIRE_BASE=origin/main python3 tools/check_file_size.py`, and
`timeout 300 python3 tools/ci_commit_gate.py check --base origin/main --head HEAD` on the committed head.
Completion requires authorized merge with CI green on the merge commit, plus the replay and live evidence.
D-5526: this seat only pushes the lane fast-forward; it never merges, marks ready, pushes main or changes PR state.
