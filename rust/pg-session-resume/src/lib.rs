//! SESSION-AUTO-CLOSE: stop an agent session when its lane is DONE and reap idle
//! sessions, recording how to resume it. See docs/SESSION-LIFECYCLE.md.

pub mod agent;
pub mod contract;
pub mod engine;
pub mod host;
pub mod lock;
pub mod persist;
pub mod record;
pub mod system;
