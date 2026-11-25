//! Pure model of the urgent-event timing of one orchestrator role: an event is one atomic
//! transition, a refused one changes nothing, and `Tick` is refused while an obligation is due.
mod invariants;
mod model;
mod params;
mod time;
mod transition;
mod types;

pub use model::Model;
pub use params::*;
pub use types::*;
