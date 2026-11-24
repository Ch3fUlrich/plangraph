//! Landing by code: the state of main, the train builder, the red-train split and the stage timeline.
pub mod build;
#[cfg(test)]
mod build_tests;
mod chain;
#[cfg(test)]
mod chain_tests;
pub mod cli;
pub mod combine;
pub mod delta;
pub mod deploy;
mod deploy_probe;
#[cfg(test)]
mod deploy_tests;
pub mod driver;
mod driver_cli;
#[cfg(test)]
mod driver_tests;
pub mod experiment;
#[cfg(test)]
mod experiment_tests;
pub mod git;
pub mod go_verify;
#[cfg(test)]
mod go_verify_tests;
pub mod main_gh;
#[cfg(test)]
mod main_gh_tests;
pub mod main_state;
#[cfg(test)]
mod main_state_tests;
pub mod parallel;
#[cfg(test)]
mod parallel_tests;
mod push;
pub mod registrar;
#[cfg(test)]
mod registrar_tests;
pub mod remerge;
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
