//! SESSION-AUTO-CLOSE: stop an agent session when its lane is DONE, resume the same
//! session on a findings file, reap idle sessions. See docs/SESSION-LIFECYCLE.md.

pub mod agent;
pub mod contract;
pub mod engine;
pub mod host;
pub mod lock;
pub mod record;
pub mod system;
