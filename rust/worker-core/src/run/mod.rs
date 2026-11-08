//! Shared run records, private persistence and execution-group supervision.
//! Unknown Python-owned fields are preserved; callers save after updating fields.
pub mod activity;
pub mod capture;
pub mod finalize;
pub mod group;
#[cfg(all(test, target_os = "linux"))]
mod group_tests;
#[cfg(target_os = "linux")]
pub mod group_unix;
#[cfg(windows)]
pub mod group_windows;
#[cfg(all(test, windows))]
mod group_windows_tests;
pub mod job_holder_windows;
pub mod livelog;
mod monitor;
#[cfg(all(test, unix))]
mod process_tests;
pub mod record;
pub mod record_io;
pub mod scrub;
pub mod supervise;
#[cfg(test)]
mod supervise_tests;
pub mod supervisor;
#[cfg(all(test, unix))]
mod supervisor_tests;
pub mod tail;
#[cfg(all(test, unix))]
pub(crate) mod testutil;

#[cfg(test)]
mod capture_tests;

#[cfg(test)]
mod record_tests;
