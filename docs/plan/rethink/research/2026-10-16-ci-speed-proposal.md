# CI speed proposal (build-L1, 2026-10-16 04:0xZ) — plangraph on workstation runners
Measured (gh api jobs, runs 38807746684 GH-hosted, 38844047043/38845394565 workst alone, 38847531104/129/143/059 workst 4 jobs at once):
| job | GH-hosted | workst alone | workst 4 concurrent |
|---|---|---|---|
| pytest | 131 s | 115-227 s | 192 s |
| pytest-postgres (pytest step) | 177 s | 164 s | 319 s |
| rust-worker | - | 147-254 s | 422 s |
| temporal-spike | - | 102 s | 317 s |
| queue | 2-3 s | 1-2 s | up to 208 s (4 slots) |
Per job: "System Python venv" step 27-29 s (no toolcache), Initialize containers 8-9 s.
Diagnosis: one box, 4 slots running together -> CPU oversubscription (pytest `-n auto` = one xdist worker per visible core per job;
cargo builds use all cores per job) + only 4 slots -> queue when >4 jobs.

Proposals (expected gain):
1. Cap parallelism per job: pytest `-n 8` (or `-n $(( $(nproc) / slots ))`), cargo `CARGO_BUILD_JOBS=8`; stops 4 jobs fighting for the same cores. (loaded pg 319 s -> ~170 s)
2. Cache the Python venv on the runner host (actions/cache keyed on requirements*.txt hash, or a pre-built venv volume mounted into runner containers). (-25 s/job)
3. Persistent cargo cache: shared CARGO_HOME registry + sccache (or per-crate target cache volume) on the workstation runners. (rust jobs: minutes -> ~1 min warm)
4. More slots: 6-8 workstation runners (32 threads, 32 GB cap) once 1+3 cap per-job CPU; ci.vm via fleet-ci as overflow (D-1390). (queue 208 s -> ~0)
5. Pre-pull service images (postgres) + Temporal CLI 1.9.1 baked into the runner image (no per-job download). (-7-15 s/job)
6. Check Docker Desktop VM CPU/RAM allocation (Settings -> Resources): if it is below 32 threads, raise it; record the value.
7. Path filters stay (#17): Rust jobs only on rust/** changes, Python jobs only on src/tests changes.
Owners: 1,2,7 = plangraph workflow files (fixes-L2-cipool is editing them for fleet-ci now); 3,4,5,6 = workstation runner image/compose (workst-L1-autoos, AutoOS repo).
