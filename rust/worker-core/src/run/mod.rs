//! Shared run records and private persistence, without process supervision.
//! Unknown Python-owned fields are preserved; callers save after updating fields.
pub mod group;
#[cfg(all(test, target_os = "linux"))]
mod group_tests;
#[cfg(target_os = "linux")]
pub mod group_unix;
pub mod record;
pub mod record_io;

#[cfg(test)]
mod record_tests;
