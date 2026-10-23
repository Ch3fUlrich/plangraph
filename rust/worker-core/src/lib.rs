//! Engine-free core of a worker that runs coding-agent attempts for cards.
//!
//! No network, no orchestration engine, no keeper calls: only worktrees,
//! checks, confinement, exit classification, retry policy and process
//! supervision.

pub mod budget;
pub mod check;
pub mod config;
pub mod confine;
pub mod exit_class;
pub mod git;
pub mod hash;
pub mod janitor;
pub mod packet;
pub mod pathjail;
pub mod probe;
pub mod redact;
pub mod sandbox_env;
pub mod scope;
pub mod stall;
pub mod worktree;
pub mod wsl;

#[cfg(test)]
pub(crate) mod testutil;
