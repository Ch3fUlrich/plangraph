//! Durable card identity, independent of a phase-scoped child workflow ID.
use crate::workflow::Card;
use temporalio_sdk::WorkflowContext;

pub(crate) fn upsert(
    ctx: &mut WorkflowContext<Card>,
    key: &str,
) -> Result<(), temporalio_common::data_converters::PayloadConversionError> {
    if ctx.patched("card-plan-key-memo-v1") {
        ctx.upsert_memo([(
            "plan_key",
            Some(temporalio_common::MemoValue::new(key.to_owned())),
        )])?;
    }
    Ok(())
}
