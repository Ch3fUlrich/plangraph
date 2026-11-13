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
pub mod cpu;
#[cfg(target_os = "linux")]
pub mod cpu_linux;
pub mod diffscan;
pub mod disk_hygiene;
pub mod events;
pub mod exit_class;
pub mod exports;
pub mod finalize;
pub mod finalize_promote;
pub mod finalize_publish;
pub mod fleet_alarm_delivery;
pub mod fleet_cap_resolver;
pub mod fleet_foundation_tick;
pub mod fleet_owner_lease;
pub mod fleet_owner_store;
pub mod fleet_owner_transition;
pub mod fleet_recycle_retain;
pub mod fleet_router_hysteresis;
pub mod fleet_router_signals;
pub mod fleet_seat_probe;
pub mod fleet_seat_supply;
pub mod fleet_series;
pub mod fleet_collect;
pub mod fleet_stale_pair_alert;
pub mod fleet_stale_pair_delivery;
pub mod fleet_window_catalog;
pub mod git;
pub mod hash;
pub mod health;
pub mod health_family;
pub mod health_rates;
pub mod health_until;
pub mod host_metrics;
pub mod host_overflow;
pub mod host_probe;
pub mod host_readiness;
pub mod host_command;
pub mod hosts;
pub mod janitor;
pub mod live_probe;
pub mod lockfile;
pub mod outcome;
pub mod packet;
pub mod package_placement;
pub mod pathjail;
pub mod placement_rule_bridge;
pub mod placement_rule_store;
pub mod placement_rules;
pub mod probe;
pub mod redact;
mod redact_tokens;
/// Minimum body length redacted for the `gh[pousr]_` token family.
pub const GITHUB_TOKEN_BODY_FLOOR: usize = redact_tokens::SPECS[1].floor;
mod redact_wrapped;
pub mod reset;
pub mod run;
pub mod sandbox_env;
mod sandbox_push;
pub mod scope;
pub mod seat_pool;
pub mod slots;
pub mod stall;
pub mod turn_budget;
pub mod watch;
pub mod workspace;
pub mod workspace_bundle;
pub mod workspace_launch;
pub mod workspace_resolver;
pub mod workspace_verify;
pub mod worktree;
pub mod writer_blob;
pub mod writer_cleanup;
pub mod writer_result;
pub mod writer_salvage;
pub mod writer_sanitize;
pub mod writer_secret;
pub mod wsl;

pub mod fleet_foundation_hysteresis;
pub mod fleet_foundation_seats;
#[cfg(test)]
pub(crate) mod testutil;
