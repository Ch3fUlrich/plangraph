# HOST-PROFILES-DRIVES-ORDER-AND-TRAITS-IN-CONFIGURATION

Outcome. Each work host has one schema-checked profile in `configuration/hosts/<host>.toml` naming its drives with roles, order and selection rule, its traits and its caps; host sync generates the unit environment from it, the build pool and worktrees pick drives by its rule, the feeder caps writers by it, placement filters and ranks by its traits, and host readiness reports drift.

- plan_source. Keeper item plangraph:HOST-PROFILES-DRIVES-ORDER-AND-TRAITS-IN-CONFIGURATION, priority 10, operator 2026-11-27 ~16:55Z; measured host setups `po0959/hosts-measured-20261127.txt` (16:46Z); rule 3.118 (D-7083). No code exists yet; no writer lane.
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- depends on. WS-WORK-DRIVES-BUILD-POOL (reviewed spec on its lane, head 46a2cf6c3): its `cargo_build_target::resolve`, list parser and `pg-host-readiness` changes are the base of slices S3, S4 and S5. Its write set also holds `overflow.py`, `seat_spawn.py`, `work_disks.rs`, `cargo_pool.rs`, `pg-host-readiness.rs`; those slices start from its merged head.
- card SHA-256. 49ac485beb9087ff88ad715488e1a615333b671792080c016b292bfe7e3cd544
- tree SHA-256. da2959d2fbd3d31c8e066506749f5798146b794307b613bec9dc96bd6100ed7d
- evidence_after_deploy. On dell, the workstation WSL and coding.vm: `profile.env` in the plangraph config directory exists with the generated header after the next scheduled `host-sync --install-release` run; `systemctl --user show pg-feeder -p Environment` and the watchdog unit carry the profile's lists, `TMPDIR` and `PLANGRAPH_MAX_WRITERS`; the hand lines in dell's feeder.env and watchdog.env (`PLANGRAPH_CARGO_TARGET_DIR`, `PLANGRAPH_CARGO_TARGET_CAP_GB`, `TMPDIR`) and coding.vm's stale `PLANGRAPH_PROBE_ROOT` are removed after a backup; `pg-host-readiness --json` shows no `profile_drift`. Env file edits on hosts are host work (rule 5.5b).

## Checked against base

- Host data lives only per host: the plangraph config directory holds `hosts.json`, `overflow-hosts.json`, `feeder.env`, `watchdog.env`; `configuration/` holds `opencode/` and `benchmark_data_pending.json`, no host data. The registry types (`HostRegistry`, `self_host`, `hosts[].caps.max_writers`, `env`, `roles`) are parsed and loaded from JSON by worker-core. [rust/worker-core/src/hosts.rs:1-140,178-195; measured file ENV sections]
- Measured 2026-11-27 16:46Z: coding.vm 12 CPU, 16 GiB, one disk `/` 45G free; dell 12 CPU, 30 GiB, Quadro P2000 5 GiB, 1000 Mb/s, `/` 35G free, `/mnt/data` 1006G free (HDD), `/tmp` tmpfs 16G, hand env lines put cargo target, cap 100 GB and TMPDIR on `/mnt/data`; workstation WSL 32 CPU, 63 GiB, RTX 3060 12 GiB, 10000 Mb/s, `/` 187G free (vhdx on C: at 91%), `/mnt/wsl/pgwork` ext4 556G free, Windows drives as 9p mounts `/mnt/c..h`; no env lines; cargo target and pool on `/`.
- Drive choice today: worktrees by `PLANGRAPH_WORKTREE_MOUNTS` most-free with a 5 GiB and percent floor; `mounted_space` refuses `/` (no parent) and any path that is not a mount point; `configured_mounts` reads every list entry as a literal path. Builds by one target (`PLANGRAPH_CARGO_TARGET_DIR`); the WS spec adds `PLANGRAPH_BUILD_MOUNTS` through `cargo_build_target::resolve` and rejects `/` as an entry (its D2). Pool slots at most `DEFAULT_POOL_SLOTS = 3` per primary. [rust/worker-core/src/work_disks.rs:13-47,60-90; rust/worker-core/src/cargo_pool.rs:27-29,97-125]
- Host sync creates missing env files and never replaces operator files. The scheduled command is `host-sync --install-release`, which returns before ordinary sync; both of its paths (`SAME release` and a fresh install) call `projections::refresh`. [rust/plangraph-cli/src/host_sync_env.rs:1-40; rust/plangraph-cli/src/host_sync.rs:78-84; rust/plangraph-cli/src/host_sync_release.rs:158-169,212-214; rust/plangraph-cli/src/host_sync_release/projections.rs:10-40]
- Feeder live admission replaces a listed host's policy cap with `running + new_writers_allowed` from RAM and load; nothing reads a configured cap after that. [rust/plangraph-cli/src/feeder/capacity.rs:149-172]
- Placement: `overflow.decide` refuses hosts on readiness, memory and disk, then ranks candidates by `placement_fallback` first (non-fallback hosts first), then by most RAM above each host's floor, then host order. The initial local shortcut and the queue retry in `seat_spawn.admit` place locally without `decide`. [src/plangraph/spawner/overflow.py:171-220; src/plangraph/spawner/seat_spawn.py:309-315,375-397]
- `overflow.py` has 799 lines, `seat_spawn.py` 791, against the 800-line cap; new Python files need an operator D-id. [tools/check_file_size.py:49; AGENTS.md:47-49]

## Decisions

- D1. Schema (TOML, `deny_unknown_fields`): `host` (registry id, equals the file stem), `max_writers` (configured cap, independent of hardware), `pool_slots`, optional `cargo_target_cap_gb`; `[traits]` `arch`, `cpus`, `ram_gb`, optional `gpu` (model), optional `gpu_mem_gb`, optional `net_mbps`, `low_power`; `[[drives]]` `path`, `roles` (non-empty subset of `build`, `worktree`, `tmp`, `cache`), `order` (unique per role), `min_free_gb`; `[selection]` one rule per role: `most-free`, `round-robin`, `ordered`, `pinned`. Validation refuses: stem/host mismatch, duplicate paths, a role without a drive, `pinned` with more than one drive, a duplicate order within a role, a non-canonical path, `max_writers` or `pool_slots` of 0, and a role that mixes `/` with another drive (D2). One parser, in Rust (`worker-core::host_profile`).
- D2. The root filesystem. `path = "/"` names the host's root filesystem and may only be the sole drive of a role. Such a role generates no mount list: its env stays unset and today's default locations under the user's home on the root filesystem apply (worktree default, `~/.cache/plangraph/cargo-target`, system `TMPDIR`). No root-level directory is provisioned, and neither `mounted_space` nor the WS resolver ever sees `/`. Drift (D7) checks `/` by free space of the user's home only.
- D3. The three profiles (pool_slots 3 and min_free_gb 5 everywhere: today's pool default and worktree floor; writer caps equal `deploy/feeder/policy.toml`):

  | host | drives (roles, order) | selection | traits | max_writers |
  | --- | --- | --- | --- | --- |
  | coding | `/` (build, worktree, tmp, cache) | all `pinned` | arch x86_64, cpus 12, ram_gb 16, no gpu, no net_mbps (unmeasured), low_power false | 3 |
  | dell | `/mnt/data` (build 1, worktree 1, tmp 1); `/` (cache) | build, worktree, tmp, cache `pinned` | arch x86_64, cpus 12, ram_gb 30, gpu "Quadro P2000", gpu_mem_gb 5, net_mbps 1000, low_power false | 5 |
  | workstation | `/mnt/wsl/pgwork` (build 1, worktree 1, tmp 1); `/` (cache) | build, worktree, tmp, cache `pinned` | arch x86_64, cpus 32, ram_gb 63, gpu "RTX 3060", gpu_mem_gb 12, net_mbps 10000, low_power false | 8 |

  dell also sets `cargo_target_cap_gb = 100` (its measured hand line). Windows 9p drives (`/mnt/c`...`/mnt/h`) are not work drives; a vhdx on such a drive appears as its ext4 mount. `ordered`, `round-robin` and `most-free` are exercised by tests and become live when a host gets a second mount.
- D4. Generation (S2). One function `host_sync_profile::generate(repo, config_dir)` resolves this host from the registry's `self_host`, loads `configuration/hosts/<id>.toml` from the plangraph checkout and replaces atomically two generated files in the plangraph config directory: `profile.env` (first line `# generated from configuration/hosts/<id>.toml at <sha>; edit the profile, not this file`) and `host-profiles.json` (every profile as JSON, with the trait tokens of D6). `profile.env` sets `PLANGRAPH_BUILD_MOUNTS`, `PLANGRAPH_WORKTREE_MOUNTS` (role drives by `order`, omitted per D2), `PLANGRAPH_BUILD_MOUNT_FLOORS`, `PLANGRAPH_WORKTREE_MOUNT_FLOORS` (same order, GB), `PLANGRAPH_BUILD_SELECT`, `PLANGRAPH_WORKTREE_SELECT`, `TMPDIR=<tmp drive>/plangraph/tmp` (omitted per D2), `PLANGRAPH_POOL_SLOTS`, `PLANGRAPH_MAX_WRITERS`, optional `PLANGRAPH_CARGO_TARGET_CAP_GB`. It is called from `projections::refresh`, so both `--install-release` paths (including `SAME release`) generate, and from ordinary `host-sync`. A host without a profile writes nothing and prints `no_host_profile <id>`. The listed units add `EnvironmentFile=-%h/.config/plangraph/profile.env` as their first env file; an operator file loaded later wins until its hand lines are removed.
- D5. Selection (S3). The WS list parser stays the one parser for both mount lists; the floors are a separate parallel list, so inventory readers (`configured_mounts`, sweep, hygiene) keep reading plain paths. `work_disks::select_with` and `cargo_build_target::resolve` take the rule and the per-entry floor (`max(5 GiB, min_free_gb)`): `most-free` (today), `ordered` (first in list order with room), `pinned` (the one entry, refused by name below its floor), `round-robin` (next entry with room after the last chosen; state `$XDG_STATE_HOME/plangraph/drive-rr-<role>` advanced under an exclusive `flock`). Unset select means `most-free`. `configured_slot_count` caps at `PLANGRAPH_POOL_SLOTS` when set, else `DEFAULT_POOL_SLOTS`.
- D6. Writer cap (S3). `capacity::apply` clamps `new_writers_allowed` so that `running_writers + new` never exceeds `PLANGRAPH_MAX_WRITERS` when set. A host without the variable keeps today's RAM-derived limit for hosts the policy lists, and 0 for unlisted hosts.
- D7. Traits (S4). The generator turns traits into tokens: `gpu` (a gpu is set), `arch:<arch>`, `fast_net` (`net_mbps >= 10000`), `low_power`. A card may name `host_traits` (list of tokens); `overflow.trait_refusal(tokens, card)` returns `traits: missing <token>` for the first missing one. `decide` refuses such hosts like a memory refusal and, among candidates, sorts `low_power` hosts after the others before the existing fallback and RAM order. The seat-spawn initial local shortcut and the queue retry's local path skip local placement when the local host's tokens miss a required one. A host without a profile has no tokens and admits only cards without `host_traits`. To stay under the size cap, `memory_refusal`, `memory_floor_record` and `_disk_refusal` move from `overflow.py` to `run_refusal.py` (placement refusals) and are re-exported from `overflow`.
- D8. Drift (S5). `pg-host-readiness --json` adds `profile_drift` findings: a configured mount drive not mounted, any drive below `min_free_gb`, `profile.env` missing or different from what D4 generates from the checkout's profile, and an operator env file setting a variable the profile generates. Readiness reports drift and never repairs it.

## Slices

| Slice | Files | Check | Depends on |
| --- | --- | --- | --- |
| S1 schema, loader, three profiles | configuration/hosts/{coding,dell,workstation}.toml, rust/worker-core/src/host_profile.rs, rust/worker-core/src/lib.rs, rust/worker-core/tests/host_profile.rs, docs/operations/host-profiles.md, changelog.d/HOST-PROFILES-DRIVES-ORDER-AND-TRAITS-IN-CONFIGURATION.md | `cargo test --manifest-path rust/Cargo.toml --locked -p pg-worker-core --test host_profile` | none |
| S2 generated env | rust/plangraph-cli/src/host_sync_profile.rs, rust/plangraph-cli/src/host_sync.rs, rust/plangraph-cli/src/host_sync_release/projections.rs, rust/plangraph-cli/tests/host_sync_profile.rs, rust/plangraph-cli/tests/host_sync_release.rs, deploy/systemd/pg-feeder.service, deploy/watchdog/watchdog.service, deploy/systemd/pg-disk-hygiene.service, deploy/systemd/pg-worktree-sweep.service, deploy/host-ready/host-ready.service | `cargo test --manifest-path rust/Cargo.toml --locked -p plangraph-cli --test host_sync_profile --test host_sync_release` | S1 |
| S3 drive rules, pool slots, writer cap | rust/worker-core/src/work_disks.rs, rust/worker-core/src/cargo_build_target.rs, rust/worker-core/src/cargo_pool.rs, rust/worker-core/tests/work_disks.rs, rust/plangraph-cli/src/feeder/capacity.rs, rust/plangraph-cli/src/feeder/runtime/live_tests.rs | `cargo test --manifest-path rust/Cargo.toml --locked -p pg-worker-core --test work_disks` and `-p plangraph-cli --lib feeder::runtime` | S1, WS merged |
| S4 placement traits | src/plangraph/spawner/overflow.py, src/plangraph/spawner/run_refusal.py, src/plangraph/spawner/seat_spawn.py, tests/test_spawner_overflow.py, docs/spawner/HOST-PROFILES-DRIVES-ORDER-AND-TRAITS-IN-CONFIGURATION.md | `python3 -m pytest -q tests/test_spawner_overflow.py` | S2, WS merged |
| S5 drift | rust/worker-core/src/host_profile.rs (drift), rust/worker-core/src/bin/pg-host-readiness.rs, rust/worker-core/tests/host_profile.rs | `cargo test --manifest-path rust/Cargo.toml --locked -p pg-worker-core --test host_profile` | S1, S2, WS merged |

Each slice is one landing; the card's write_set is the union and its `line_caps` bound every file.

## Entry points

- E1. `host_profile::load(path)` / `load_all(dir)` and validation.
- E2. `host_sync_profile::generate` via `host-sync --install-release` (both paths) and ordinary `host-sync`; units loading `profile.env`.
- E3. `work_disks::worktree_disk_with`, `cargo_build_target::resolve`, `cargo_pool::configured_slot_count`, `feeder::capacity::apply`.
- E4. `seat_spawn.admit` (local shortcut, queue retry) and `overflow.decide`.
- E5. `pg-host-readiness --json` `profile_drift`.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | The three checked-in profiles load with the D3 values; each invalid case of D1 is refused naming the field. | X1 |
| B2 | E2 | A `SAME release` run and a fresh install both write `profile.env` and `host-profiles.json`; coding's profile writes no mount list and no `TMPDIR`; regeneration from an unchanged profile is byte-identical; a host without a profile writes nothing and says so. | X2 |
| B3 | E3 | Through the real worktree and build entry points: two drives with `round-robin` alternate across calls; `ordered` picks the first with room; `pinned` below its floor refuses naming it; an unset list on coding's profile keeps today's default; slots cap at `PLANGRAPH_POOL_SLOTS`. | X3 |
| B4 | E3 | With RAM for 6 more writers, 2 running and `PLANGRAPH_MAX_WRITERS=5`, live admission allows 3; without the variable it allows 6. | X4 |
| B5 | E4 | A healthy local host without a GPU does not place a `gpu` card through the normal local shortcut or the queue retry; `decide` places it on a host with the token; a low-power host ranks last at equal admission. | X5 |
| B6 | E5 | A missing mount drive and an env file differing from the profile are `profile_drift` findings. | X6 |

## Verification

- X1. `rust/worker-core/tests/host_profile.rs::the_checked_in_profiles_load` and `::each_invalid_profile_is_refused_naming_the_field`.
- X2. `rust/plangraph-cli/tests/host_sync_profile.rs::the_generated_env_carries_the_profile_paths`, `::a_root_only_profile_writes_no_mount_list`, `::a_host_without_a_profile_writes_nothing`; `rust/plangraph-cli/tests/host_sync_release.rs::same_release_also_generates_the_host_profile`.
- X3. `rust/worker-core/tests/work_disks.rs::round_robin_alternates_between_two_build_drives`, `::ordered_selects_the_first_drive_with_room`, `::pinned_below_its_floor_is_refused_by_name`, `::a_root_only_role_keeps_the_default`, `::pool_slots_cap_at_the_profile_value` (fake `DiskSpace` sampler for selection; the build cases call `cargo_build_target::resolve`).
- X4. `rust/plangraph-cli/src/feeder/runtime/live_tests.rs::proc_fixture::the_profile_writer_cap_clamps_the_ram_budget`.
- X5. `tests/test_spawner_overflow.py::test_gpu_card_is_not_placed_on_a_local_host_without_gpu`, `::test_gpu_card_skips_hosts_without_the_token`, `::test_low_power_host_ranks_last`.
- X6. `rust/worker-core/tests/host_profile.rs::a_missing_drive_and_a_stale_env_are_drift`.

Each slice writes its tests first; on the base they fail to compile or fail (no module, no rule, no clamp, no trait filter).

## Scope and reuse

- R1. Reuse `HostRegistry::self_host`, the WS list parser and resolver, `select_with`, `DiskSpace`, host sync's atomic writes and `projections::refresh`, the readiness JSON report, `overflow.decide`, `capacity::apply`.
- N1. Out of scope: generating `hosts.json` from the repository (FLEET-REGISTRY-ONE-SOURCE-GENERATED-AT-INSTALL; its `caps.max_writers` reads the profile when it lands), CPU-load and disk inputs of placement (PLACEMENT-READS-CPU-LOAD-AND-DISK-ON-THE-SPAWNER-PATH), installing units and editing host env files (host work), Windows-side drive management.

## Review r1

- WRONG .md:14 and .md:18 fixed (hosts.rs:178-195, overflow ordering stated). D2, D3 (release paths, writer cap), D4 (resolver, floors list), D5 (local paths), D7 (line caps, existing test file) applied above.

## Open points for design approval

None.

## Open review points for the implementer (round 2, final under rule 3.119, D-7092)

The spec is final after two design review rounds (gpt-6.2-sol-high). The points below were still open; the implementer resolves them inside the write set or reports why not.

VERDICT: DESIGN-CHANGE
CITATIONS: checked 12, wrong 2
WRONG .md:19 | src/plangraph/spawner/overflow.py:183,206–213 | Tie order is alphabetical in ordinary overflow and configured host order in remote-first mode. In local comparison, local wins an otherwise equal tie.
WRONG .md:20 | tools/check_file_size.py:47–50 | Cite line 47 for the 800-line cap. Line 49 describes documentation headroom.
ROUND 1: Registry citation, release hook, writer cap, build resolver and plain-path parsing, and D7 budgets and Python test placement fixed. D2 admission proof and D5 local trait enforcement remain open.
FIT AND ARTIFACTS: The three profiles, four drive roles, cap, traits and drift remain in scope. All 30 tree writes match the card. Both digests and current-head line budgets check out.
POINTS: D2 | .md:25,67,76 | rust/pg-spawnd/src/native_writer_commands.rs:116–123 | An unset-list selector test can pass by returning `None` without proving coding can launch. Add a native admission test using coding’s generated environment and assert the resulting worktree is under the default run directory. BLOCKS
POINTS: D8 | .md:35,66,75 | rust/plangraph-cli/src/host_sync.rs:114–128,256–270 | Ordinary sync exits early when tools are present and otherwise resolves a private data checkout. Specify how it finds the Plangraph checkout and runs generation before the early return. Assert generation through ordinary sync, fresh install and `SAME release`. BLOCKS
POINTS: D9 | .md:35–36,46 | src/plangraph/spawner/overflow.py:470–472,507–510; src/plangraph/spawner/host_ready_source.py:351–367 | Loading `profile.env` in the listed units leaves SSH/WSL launches and probes using the registry env file alone. Specify target-side loading before operator overrides and test the environment received by a remote launch and probe. BLOCKS
POINTS: D10 | .md:24,31–36,67 | rust/worker-core/src/work_disks.rs:49–50,82–90; docs/operations/worker-disks.md:50–53 | The schema offers selection for tmp and cache, but generation only defines build/worktree selection and emits a TMPDIR whose directory may be absent. Define the supported tmp/cache rules and consumers, document who provisions and verifies the writable worktree and tmp directories, and test the generated paths. BLOCKS
POINTS: D5 | .md:38,69,78 | src/plangraph/spawner/overflow.py:277–298; src/plangraph/spawner/seat_spawn.py:338–366,375–397 | Specify how local and remote registry IDs resolve tokens from `host-profiles.json`. Feed a missing local trait into shared local health so compare-local also refuses when `decide` returns no host. Test that case alongside the shortcut and queue retry. BLOCKS
