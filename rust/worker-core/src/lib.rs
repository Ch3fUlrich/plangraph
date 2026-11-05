//! Engine-free core of a worker that runs coding-agent attempts for cards.
//!
//! No network, no orchestration engine, no keeper calls: only worktrees,
//! checks, confinement, exit classification, retry policy and process
//! supervision.

pub mod activity;
pub mod budget;
pub mod check;
pub mod config;
pub mod confine;
pub mod diffscan;
pub mod events;
pub mod exit_class;
pub mod exports;
pub mod fleet_owner_lease;
pub mod fleet_owner_transition;
pub mod fleet_router_signals;
pub mod fleet_window_catalog;
pub mod git;
pub mod hash;
pub mod health;
pub mod host_probe;
pub mod hosts;
pub mod janitor;
pub mod lockfile;
pub mod outcome;
pub mod packet;
pub mod pathjail;
pub mod probe;
pub mod redact;
pub mod reset;
pub mod run;
pub mod sandbox_env;
pub mod scope;
pub mod slots;
pub mod stall;
pub mod workspace;
pub mod workspace_resolver;
pub mod worktree;
pub mod wsl;

#[cfg(test)]
pub(crate) mod testutil;
