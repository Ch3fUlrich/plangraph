- `CARGO-TARGET-POOL`: Add leased Cargo target directory pool (`cargo_pool`) with stale-lease reaping, per-slot/N×cap enforcement, and lock-wait telemetry.
- `CARGO-TARGET-POOL` (fix): fast-checks size the pool from free disk, trim idle slots over the cap and record real Cargo lock-wait milliseconds per cargo step; `pg-disk-hygiene` and the disk-refusal cleanup cover the pool slots; the cap default is one value (12 GiB) in Python and Rust.
Disk admission carries the admitted checkout into immediate and queued launches and also checks the Cargo and state filesystems.
An exact checkout keeps its configured floor even when other configured checkouts share the same device.
Fast-checks waits briefly for a leased Cargo pool slot to free up. Disk admission explains when no configured drive has a launchable Git checkout.
