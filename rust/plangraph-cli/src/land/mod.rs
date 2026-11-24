//! Landing by code: the state of main, the train builder, the red-train split and the stage timeline.
pub mod build;
#[cfg(test)]
mod build_tests;
mod chain;
pub mod cli;
pub mod delta;
pub mod deploy;
mod deploy_probe;
#[cfg(test)]
mod deploy_tests;
pub mod git;
pub mod go_statement;
#[cfg(test)]
mod go_statement_tests;
pub mod go_verify;
#[cfg(test)]
mod go_verify_schema_tests;
#[cfg(test)]
mod go_verify_tests;
pub mod keeper_schema;
#[cfg(test)]
mod keeper_schema_tests;
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
