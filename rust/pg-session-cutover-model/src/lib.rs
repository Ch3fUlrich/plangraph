//! Pure model of the urgent-event timing of one orchestrator role on the digest wait.
//! Each successful event is one atomic transition; a refused event changes nothing.
//! Time is a `Tick` of `TICK_SECS`; a tick is refused while an obligation is due, so a scan,
//! wake, takeover, alarm or supervisor pass cannot be skipped by letting time pass.
//! Recycle, adoption, session slots and the digest window belong to later slices.
mod invariants;
mod model;
mod params;
mod time;
mod transition;
mod types;

pub use model::Model;
pub use params::*;
pub use types::*;
