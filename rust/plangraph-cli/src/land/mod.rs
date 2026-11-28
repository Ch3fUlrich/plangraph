//! Landing by code: the state of main, the train builder, the red-train split and the stage timeline.
pub mod build;
#[cfg(test)]
mod build_tests;
mod chain;
pub mod cli;
#[cfg(test)]
mod cut_check_tests;
mod cut_gate;
#[cfg(test)]
mod cut_preflight_tests;
pub mod delta;
pub mod deploy;
pub mod deploy_apply;
#[cfg(test)]
mod deploy_apply_tests;
pub mod deploy_derive;
#[cfg(test)]
mod deploy_derive_tests;
pub mod deploy_host;
mod deploy_plan_cli;
pub mod deploy_post;
#[cfg(test)]
mod deploy_post_cleanup_tests;
#[cfg(test)]
mod deploy_post_tests;
mod deploy_probe;
pub mod deploy_run;
#[cfg(test)]
mod deploy_tests;
pub mod deploy_train;
pub mod git;
pub mod go_verify;
#[cfg(test)]
mod go_verify_tests;
pub mod lane_cleanup;
mod lane_guard;
mod lane_probe;
mod lane_proof;
pub mod main_gh;
#[cfg(test)]
mod main_gh_tests;
pub mod main_state;
#[cfg(test)]
mod main_state_tests;
mod push;
pub mod registrar;
#[cfg(test)]
mod registrar_tests;
pub mod resolve;
#[cfg(test)]
mod resolve_tests;
pub mod split;
#[cfg(test)]
mod split_tests;
pub mod timeline;
#[cfg(test)]
mod timeline_tests;
pub mod train;
#[cfg(test)]
mod train_tests;
