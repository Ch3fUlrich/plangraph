use pg_worker_core::config::Config;
use std::collections::BTreeSet;
use std::sync::{Mutex, OnceLock};
use temporalio_sdk::activities::ActivityError;

fn refusal(kind: &str, key: &str) -> ActivityError {
    ActivityError::application(
        temporalio_sdk::ApplicationFailure::builder(anyhow::anyhow!("{kind}: {key}"))
            .type_name(kind.to_owned())
            .non_retryable(true)
            .build(),
    )
}
#[derive(Debug, thiserror::Error)]
pub enum AdmissionReason {
    #[error("not allowlisted: {0}")]
    NotAllowlisted(String),
    #[error("not claimable: {0}")]
    NotClaimable(String),
    #[error("attempt already active: {0}")]
    Overlap(String),
}
/// The `type_name` of each typed admission refusal: the one definition both the
/// worker (which emits it) and the workflow (which recognizes it) use.
const NOT_ALLOWLISTED: &str = "card_not_allowlisted";
const NOT_CLAIMABLE: &str = "card_not_claimable";
const OVERLAP: &str = "card_attempt_overlap";
pub(crate) const REFUSAL_KINDS: [&str; 3] = [NOT_ALLOWLISTED, NOT_CLAIMABLE, OVERLAP];
impl AdmissionReason {
    fn kind(&self) -> &'static str {
        match self {
            Self::NotAllowlisted(_) => NOT_ALLOWLISTED,
            Self::NotClaimable(_) => NOT_CLAIMABLE,
            Self::Overlap(_) => OVERLAP,
        }
    }
}
#[derive(Debug, thiserror::Error)]
pub(crate) enum EntryError {
    #[error("card admission refused: {0}")]
    Admission(#[from] AdmissionReason),
    #[error(transparent)]
    Attempt(#[from] crate::attempt::AttemptError),
}
pub(crate) fn attempt_error(error: EntryError) -> ActivityError {
    if let EntryError::Admission(reason)
    | EntryError::Attempt(crate::attempt::AttemptError::Admission(reason)) = &error
    {
        refusal(reason.kind(), &reason.to_string())
    } else {
        // Git stderr and io errors can carry credentials: only the redacted
        // text reaches Temporal history.
        let message = crate::output_redact::failure_text(&error.to_string());
        ActivityError::application(
            temporalio_sdk::ApplicationFailure::builder(anyhow::anyhow!("{message}")).build(),
        )
    }
}
pub(crate) fn allow(cfg: &Config, key: &str) -> Result<(), ActivityError> {
    if cfg.card_allowed(key) {
        Ok(())
    } else {
        Err(attempt_error(
            AdmissionReason::NotAllowlisted(key.into()).into(),
        ))
    }
}
pub(crate) fn claimed(
    claim: Option<crate::keeper::Claim>,
    key: &str,
) -> Result<Option<crate::keeper::Claim>, ActivityError> {
    claim
        .map(Some)
        .ok_or_else(|| attempt_error(AdmissionReason::NotClaimable(key.into()).into()))
}
fn owners() -> &'static Mutex<BTreeSet<String>> {
    static OWNERS: OnceLock<Mutex<BTreeSet<String>>> = OnceLock::new();
    OWNERS.get_or_init(Mutex::default)
}
pub(crate) struct Owner(String);
impl Drop for Owner {
    fn drop(&mut self) {
        owners()
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .remove(&self.0);
    }
}
pub(crate) fn enter(cfg: &Config, key: &str) -> Result<Owner, AdmissionReason> {
    let mut owners = owners().lock().unwrap_or_else(|e| e.into_inner());
    if !cfg.card_allowed(key) {
        return Err(AdmissionReason::NotAllowlisted(key.into()));
    }
    if !owners.insert(key.to_owned()) {
        return Err(AdmissionReason::Overlap(key.into()));
    }
    Ok(Owner(key.to_owned()))
}

pub(crate) fn run_attempt(
    harness: Option<&dyn crate::harness::Harness>,
    cfg: &Config,
    patterns: &pg_worker_core::exit_class::Patterns,
    input: &crate::attempt::AttemptInput,
    cancelled: &dyn Fn() -> bool,
) -> Result<crate::attempt::AttemptResult, EntryError> {
    Ok(crate::attempt::run_attempt_with(
        harness, cfg, patterns, input, cancelled,
    )?)
}

#[cfg(test)]
#[path = "card_admit_tests.rs"]
pub(crate) mod tests;
