//! Engine-free core of a worker that runs coding-agent attempts for cards.
//!
//! No network, no orchestration engine, no keeper calls: only worktrees,
//! checks, confinement, exit classification, retry policy and process
//! supervision.

pub mod budget;
pub mod check;
pub mod config;
pub mod confine;
pub mod diffscan;
pub mod exit_class;
pub mod exports;
pub mod git;
pub mod hash;
pub mod host_probe;
pub mod hosts;
pub mod janitor;
pub mod lockfile;
pub mod packet;
pub mod pathjail;
pub mod probe;
pub mod redact;
pub mod run;
pub mod sandbox_env;
pub mod scope;
pub mod slots;
pub mod stall;
pub mod workspace;
pub mod worktree;
pub mod wsl;

#[cfg(test)]
pub(crate) mod testutil;
