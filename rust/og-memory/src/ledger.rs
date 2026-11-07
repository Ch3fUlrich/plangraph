//! Payload-free blocked-ledger types and retention limits.
use super::Error;

pub(super) const INVALID_SCOPE: &str = "<invalid-scope>";
pub(super) const BLOCKED_LIMIT: usize = 128;

/// Payload-free ledger entries: validation-rejected content is never retained.
/// Transport verification failures may refer to an already committed fact.
#[derive(Debug)]
pub struct Blocked {
    pub project: String,
    pub reason: Error,
}
