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
pub(crate) enum EntryError {
    #[error("card admission refused: {0}")]
    Admission(String),
    #[error(transparent)]
    Attempt(#[from] crate::attempt::AttemptError),
}
pub(crate) fn attempt_error(error: EntryError) -> ActivityError {
    if matches!(&error, EntryError::Admission(_)) {
        refusal("card_admission_refused", &error.to_string())
    } else {
        ActivityError::application(
            temporalio_sdk::ApplicationFailure::builder(anyhow::anyhow!("{error}")).build(),
        )
    }
}
pub(crate) fn allow(cfg: &Config, key: &str) -> Result<(), ActivityError> {
    if cfg.card_allowed(key) {
        Ok(())
    } else {
        Err(refusal("card_not_allowlisted", key))
    }
}
pub(crate) fn claimed(
    claim: Option<crate::keeper::Claim>,
    key: &str,
) -> Result<Option<crate::keeper::Claim>, ActivityError> {
    claim
        .map(Some)
        .ok_or_else(|| refusal("card_not_claimable", key))
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
pub(crate) fn enter(cfg: &Config, key: &str) -> Result<Owner, EntryError> {
    let mut owners = owners().lock().unwrap_or_else(|e| e.into_inner());
    if !cfg.card_allowed(key) || !owners.insert(key.to_owned()) {
        return Err(EntryError::Admission(key.to_owned()));
    }
    Ok(Owner(key.to_owned()))
}

pub(crate) fn run_attempt(
    harness: Option<&dyn crate::harness::Harness>,
    cfg: &Config,
    patterns: &pg_worker_core::exit_class::Patterns,
    input: Option<&crate::attempt::AttemptInput>,
    cancelled: &dyn Fn() -> bool,
) -> Result<crate::attempt::AttemptResult, EntryError> {
    let input = input.ok_or_else(|| EntryError::Admission("no claimed card".into()))?;
    let _owner = enter(cfg, &input.claim.card)?;
    Ok(crate::attempt::run_attempt_with(
        harness, cfg, patterns, input, cancelled,
    )?)
}

#[cfg(test)]
#[path = "card_admit_tests.rs"]
pub(crate) mod tests;
