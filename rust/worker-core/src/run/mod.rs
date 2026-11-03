//! Shared run records and private persistence, without process supervision.
//! Unknown Python-owned fields are preserved; callers save after updating fields.
pub mod record;
pub mod record_io;

#[cfg(test)]
mod record_tests;
