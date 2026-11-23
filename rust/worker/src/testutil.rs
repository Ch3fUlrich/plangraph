//! Test helpers shared by the unit tests of this crate.

#[cfg(unix)]
pub use pg_worker_core::executable_test_support::write_script;
