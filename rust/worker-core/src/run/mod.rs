//! Shared run records and private persistence, without process supervision.
//! Unknown Python-owned fields are preserved; callers save after updating fields.
pub mod activity;
pub mod capture;
pub mod group;
#[cfg(all(test, target_os = "linux"))]
mod group_tests;
#[cfg(target_os = "linux")]
pub mod group_unix;
pub mod livelog;
pub mod record;
pub mod record_io;
pub mod scrub;
pub mod tail;

#[cfg(test)]
mod capture_tests;

#[cfg(test)]
mod record_tests;
