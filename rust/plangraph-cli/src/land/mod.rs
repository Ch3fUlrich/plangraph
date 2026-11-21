//! Landing by code: the state of main, the train builder, the red-train split and the stage timeline.
pub mod build;
#[cfg(test)]
mod build_tests;
pub mod cli;
pub mod git;
pub mod main_gh;
#[cfg(test)]
mod main_gh_tests;
pub mod main_state;
#[cfg(test)]
mod main_state_tests;
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
