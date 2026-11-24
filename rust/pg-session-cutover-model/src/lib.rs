//! Pure model of one orchestrator role switching from today's waits to the digest wait.
//! Each successful event is one atomic transition; a refused event changes nothing.
//! Time is a `Tick` of `TICK_SECS`; a tick is refused while an obligation is due, so an
//! obligation (scan, wake, receipt, takeover, supervision) cannot be skipped by letting time pass.
mod invariants;
mod model;
mod params;
mod recycle;
mod time;
mod transition;
mod types;

pub use model::{Digest, Model};
pub use params::*;
pub use types::*;
