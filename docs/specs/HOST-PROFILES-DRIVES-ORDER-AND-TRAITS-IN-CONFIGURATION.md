# HOST-PROFILES-DRIVES-ORDER-AND-TRAITS-IN-CONFIGURATION

Outcome. Each work host has one schema-checked profile in `configuration/hosts/<host>.toml` naming its drives with roles, order and selection rule, its traits and its caps; host sync generates the unit environment from it, the build pool and worktrees pick drives by its rule, placement filters and ranks by its traits, and host readiness reports drift.

- plan_source. Keeper item plangraph:HOST-PROFILES-DRIVES-ORDER-AND-TRAITS-IN-CONFIGURATION, priority 10, operator 2026-11-27 ~16:55Z; measured host setups `po0959/hosts-measured-20261127.txt` (16:46Z); rule 3.118 (D-7083). No code exists yet; no writer lane.
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- depends on. WS-WORK-DRIVES-BUILD-POOL (build mount list, `pg-host-readiness --cargo-target`) for slices S3 and S5.
- card SHA-256. f6359b415c2d16f0ecef09e5c96d45ed59fcaa25a51f80e942f063d30fdfdb42
- tree SHA-256. 87eae09140db2d5c2eba89972e83a17da2cbbf9c7fdcbaa0f98307938581a5de
- evidence_after_deploy. On dell, the workstation WSL and coding.vm: `~/.config/plangraph/profile.env` exists with the generated header, `systemctl --user show pg-feeder -p Environment` and the watchdog unit carry the profile's `PLANGRAPH_BUILD_MOUNTS`, `PLANGRAPH_WORKTREE_MOUNTS`, `TMPDIR`; the hand lines in dell's feeder.env and watchdog.env (`PLANGRAPH_CARGO_TARGET_DIR`, `PLANGRAPH_CARGO_TARGET_CAP_GB`, `TMPDIR`) and coding.vm's stale `PLANGRAPH_PROBE_ROOT` are removed after a backup; `pg-host-readiness --json` shows no `profile_drift`. Env file edits on hosts are host work (rule 5.5b).

## Checked against base

- Host data lives only per host: `~/.config/plangraph/{hosts.json, overflow-hosts.json, feeder.env, watchdog.env}`; `configuration/` holds `opencode/` and `benchmark_data_pending.json`, no host data. The registry type `HostRegistry` (`self_host`, `hosts[].caps.max_writers`, `env`, `roles`) is parsed by worker-core. [rust/worker-core/src/hosts.rs:1-140; measured file ENV sections]
- Measured 2026-11-27 16:46Z: coding.vm 12 CPU, 16 GiB, one disk `/` 45G free; dell 12 CPU, 30 GiB, Quadro P2000 5 GiB, 1000 Mb/s, `/` 35G free, `/mnt/data` 1006G free (HDD), `/tmp` tmpfs 16G, hand env lines put cargo target, cap 100 GB and TMPDIR on `/mnt/data`; workstation WSL 32 CPU, 63 GiB, RTX 3060 12 GiB, 10000 Mb/s, `/` 187G free (vhdx on C: at 91%), `/mnt/wsl/pgwork` ext4 556G free, Windows drives as 9p mounts `/mnt/c..h`; no env lines; cargo target and pool on `/`.
- Drive choice today: worktrees by `PLANGRAPH_WORKTREE_MOUNTS` most-free with a 5 GiB and percent floor; builds by one target (`PLANGRAPH_CARGO_TARGET_DIR`), a mount list after WS-WORK-DRIVES-BUILD-POOL; pool slots at most `DEFAULT_POOL_SLOTS = 3` per primary. [rust/worker-core/src/work_disks.rs:10-129; rust/worker-core/src/cargo_pool.rs:27-29,97-125]
- Host sync creates missing environment files and never replaces operator files; it runs by timer on every host. [rust/plangraph-cli/src/host_sync_env.rs:1-40; rust/plangraph-cli/src/host_sync.rs]
- Placement ranks admitted remote hosts by most free RAM after memory and disk refusals; no trait input. [src/plangraph/spawner/overflow.py:146-220]
- Units load `plangraph.env`, `watchdog.env`, `disk-hygiene.env` through `EnvironmentFile=`. [deploy/systemd/pg-feeder.service:10; deploy/watchdog/watchdog.service:8; deploy/systemd/pg-disk-hygiene.service:6-9; deploy/systemd/pg-worktree-sweep.service:6; deploy/host-ready/host-ready.service:9]

## Decisions

- D1. Schema (TOML, `deny_unknown_fields`): `host` (registry id, equals the file stem), `max_writers` (configured cap, independent of hardware), `pool_slots` (cap of build pool slots per build root), optional `cargo_target_cap_gb`; `[traits]` `arch`, `cpus`, `ram_gb`, optional `gpu` (model) and `gpu_mem_gb`, optional `net_mbps`, `low_power` (bool); `[[drives]]` `path` (canonical absolute), `roles` (non-empty subset of `build`, `worktree`, `tmp`, `cache`), `order` (integer, unique per role), `min_free_gb`; `[selection]` one rule per role in use: `most-free`, `round-robin`, `ordered`, `pinned`. Validation refuses: stem/host mismatch, duplicate paths, a role with no drive, `pinned` with more than one drive for the role, duplicate order within a role, relative or non-canonical paths, `max_writers` or `pool_slots` of 0. One parser in Rust (`worker-core::host_profile`); nothing else parses the TOML.
- D2. Profiles from the measured data: `coding` (one drive `/`, all roles, min_free_gb 5, `max_writers = 3`), `dell` (`/mnt/data` build+worktree+tmp order 1, `/` worktree order 2 and cache, `cargo_target_cap_gb = 100`, `max_writers = 5`), `workstation` (`/mnt/wsl/pgwork` all roles order 1, `/` worktree+cache order 2, `max_writers = 8`). `pool_slots = 3` and `min_free_gb = 5` everywhere: the current pool default and worktree floor, changed later only by measurement. Windows 9p drives (`/mnt/c`...`/mnt/h`) are not work drives (9p, no `mountpoint` identity); a vhdx on such a drive appears as its ext4 mount. Writer caps equal `deploy/feeder/policy.toml` today.
- D3. Generation (S2). Host sync resolves this host's id from the registry `self_host`, loads `configuration/hosts/<id>.toml` from the plangraph checkout it syncs, and writes, replacing them atomically, two generated files: `~/.config/plangraph/profile.env` (first line `# generated from configuration/hosts/<id>.toml at <sha>; edit the profile, not this file`) with `PLANGRAPH_BUILD_MOUNTS`, `PLANGRAPH_WORKTREE_MOUNTS` (role drives by `order`), `PLANGRAPH_BUILD_SELECT`, `PLANGRAPH_WORKTREE_SELECT`, `TMPDIR=<tmp drive>/plangraph/tmp` (rule applied at generation), `PLANGRAPH_POOL_SLOTS`, optional `PLANGRAPH_CARGO_TARGET_CAP_GB`, `PLANGRAPH_MAX_WRITERS`; and `~/.config/plangraph/host-profiles.json` (every profile as JSON, for Python readers). A host without a profile writes nothing and reports `no_host_profile`. The listed units add `EnvironmentFile=-%h/.config/plangraph/profile.env` as their first env file, so an operator file loaded later still wins until its hand lines are removed.
- D4. Selection (S3). `work_disks::select_with` takes the rule: `most-free` (today), `ordered` (first by order with room), `pinned` (the one drive, refused by name when below its floor), `round-robin` (the next drive with room after the last chosen, state in `$XDG_STATE_HOME/plangraph/drive-rr-<role>`, read and advanced under an exclusive `flock`). Per-drive `min_free_gb` comes through the mount list as `path=GB` entries (`/mnt/data=5`); a bare path keeps the 5 GiB floor. `configured_slot_count` caps at `PLANGRAPH_POOL_SLOTS` when set, else `DEFAULT_POOL_SLOTS`.
- D5. Traits (S4). Placement reads `host-profiles.json`; a card may name `host_traits` tokens: `gpu`, `arch:<arch>`, `net_mbps:<min>`, `cpus:<min>`; a host lacking one is refused `traits: <token>`. Among admitted hosts, `low_power = true` ranks after the others; otherwise the existing free-RAM order holds. A host with no profile admits only cards without `host_traits`.
- D6. Drift (S5). `pg-host-readiness --json` adds `profile_drift` findings: a configured drive not mounted or missing, a drive below `min_free_gb`, `profile.env` missing or not equal to what D3 generates from the checkout's profile, an operator env file setting a variable the profile generates. Drift is reported, never repaired by readiness.

## Slices

| Slice | Files | Check | Depends on |
| --- | --- | --- | --- |
| S1 schema, loader, three profiles | configuration/hosts/{coding,dell,workstation}.toml, rust/worker-core/src/host_profile.rs, rust/worker-core/src/lib.rs, rust/worker-core/tests/host_profile.rs, docs/operations/host-profiles.md, changelog.d/HOST-PROFILES-DRIVES-ORDER-AND-TRAITS-IN-CONFIGURATION.md | `cargo test --manifest-path rust/Cargo.toml --locked -p pg-worker-core --test host_profile` | none |
| S2 generated env | rust/plangraph-cli/src/host_sync_profile.rs, rust/plangraph-cli/src/host_sync.rs, rust/plangraph-cli/tests/host_sync_profile.rs, deploy/systemd/pg-feeder.service, deploy/watchdog/watchdog.service, deploy/systemd/pg-disk-hygiene.service, deploy/systemd/pg-worktree-sweep.service, deploy/host-ready/host-ready.service | `cargo test --manifest-path rust/Cargo.toml --locked -p plangraph-cli --test host_sync_profile` | S1 |
| S3 drive rules and pool slots | rust/worker-core/src/work_disks.rs, rust/worker-core/src/cargo_pool.rs, rust/worker-core/tests/work_disks.rs | `cargo test --manifest-path rust/Cargo.toml --locked -p pg-worker-core --test work_disks` | S1, WS-WORK-DRIVES-BUILD-POOL merged |
| S4 placement traits | src/plangraph/spawner/overflow.py, tests/test_overflow_traits.py, docs/spawner/HOST-PROFILES-DRIVES-ORDER-AND-TRAITS-IN-CONFIGURATION.md | `python3 -m pytest -q tests/test_overflow_traits.py` | S2 |
| S5 drift | rust/worker-core/src/host_profile.rs (drift), rust/worker-core/src/bin/pg-host-readiness.rs, rust/worker-core/tests/host_profile.rs | `cargo test --manifest-path rust/Cargo.toml --locked -p pg-worker-core --test host_profile` | S1, S2, WS-WORK-DRIVES-BUILD-POOL merged |

Each slice is one landing; the card's write_set is the union. S3 and S5 edit files that WS-WORK-DRIVES-BUILD-POOL also writes and start from its merged head.

## Entry points

- E1. `host_profile::load(path)` / `load_all(dir)` and validation.
- E2. `plangraph host-sync` writing `profile.env` and `host-profiles.json`; units loading `profile.env`.
- E3. `work_disks` selection and `cargo_pool::configured_slot_count`.
- E4. `overflow.decide` with `host_traits`.
- E5. `pg-host-readiness --json` `profile_drift`.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | The three checked-in profiles load; each invalid case of D1 is refused naming the field. | X1 |
| B2 | E2 | The generated env carries the profile paths, rules, TMPDIR, pool slots and cap; a host without a profile writes nothing and says so; regeneration from an unchanged profile is byte-identical. | X2 |
| B3 | E3 | Two build drives with `round-robin` are chosen alternately across calls; `ordered` picks the first with room; `pinned` below its floor refuses naming it; slots cap at `PLANGRAPH_POOL_SLOTS`. | X3 |
| B4 | E4 | A `gpu` card is refused on coding.vm and placed on dell or the workstation; a low-power host ranks last at equal admission. | X4 |
| B5 | E5 | A missing drive and an env file differing from the profile are `profile_drift` findings. | X5 |

## Verification

- X1. `rust/worker-core/tests/host_profile.rs::the_checked_in_profiles_load` and `::each_invalid_profile_is_refused_naming_the_field`.
- X2. `rust/plangraph-cli/tests/host_sync_profile.rs::the_generated_env_carries_the_profile_paths` and `::a_host_without_a_profile_writes_nothing`.
- X3. `rust/worker-core/tests/work_disks.rs::round_robin_alternates_between_two_build_drives`, `::ordered_selects_the_first_drive_with_room`, `::pinned_below_its_floor_is_refused_by_name`, `::pool_slots_cap_at_the_profile_value`.
- X4. `tests/test_overflow_traits.py::test_gpu_card_skips_hosts_without_gpu` and `::test_low_power_host_ranks_last`.
- X5. `rust/worker-core/tests/host_profile.rs::a_missing_drive_and_a_stale_env_are_drift`.

Each slice writes its tests first; on the base they fail to compile or fail (no module, no rule, no trait filter).

## Scope and reuse

- R1. Reuse `HostRegistry::self_host`, `select_with`, `DiskSpace`, the `mountpoint` check, host sync's atomic writes, the readiness JSON report, `overflow.decide`.
- N1. Out of scope: generating `hosts.json` from the repository (FLEET-REGISTRY-ONE-SOURCE-GENERATED-AT-INSTALL; its `caps.max_writers` reads the profile when it lands), CPU-load and disk inputs of placement (PLACEMENT-READS-CPU-LOAD-AND-DISK-ON-THE-SPAWNER-PATH), installing units and editing host env files (host work), Windows-side drive management.

## Open points for design approval

None.
