//! Payload-free blocked-ledger types and retention limits.
use super::Error;

pub(super) const INVALID_SCOPE: &str = "<invalid-scope>";
pub(super) const BLOCKED_LIMIT: usize = 128;

pub(super) fn reason(error: &Error) -> Error {
    match error {
        Error::Refused(r) => Error::Refused(r.clone()),
        Error::Outage => Error::Outage,
        Error::Unavailable => Error::Unavailable,
        Error::Conflict => Error::Conflict,
        Error::WritesDisabled => Error::WritesDisabled,
        Error::InvalidScope => Error::InvalidScope,
        Error::Unknown => Error::Unknown,
        Error::Unverified => Error::Unverified,
    }
}

/// Payload-free ledger entries: validation-rejected content is never retained.
/// Transport verification failures may refer to an already committed fact.
#[derive(Debug)]
pub struct Blocked {
    pub project: String,
    pub reason: Error,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ledger_preserves_every_error_reason() {
        for error in [
            Error::Refused(crate::Refusal::UnknownKind),
            Error::Outage,
            Error::Unavailable,
            Error::Conflict,
            Error::Unknown,
            Error::Unverified,
            Error::WritesDisabled,
            Error::InvalidScope,
        ] {
            assert_eq!(reason(&error), error);
        }
    }
}
