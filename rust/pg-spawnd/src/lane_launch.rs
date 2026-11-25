//! The admission of one writer launch: a check of its keys against the live runs of the one
//! state path the entry point resolved. Nothing is reserved.

use crate::lane_guard::{admit, Observed, Refusal};
use std::path::Path;

/// One writer launch to admit.
pub struct LaunchRequest<'a> {
    pub prompt: &'a str,
    /// Keys the launch card names besides the brief's own.
    pub extra: &'a [String],
    /// The one state path of the entry point.
    pub state: &'a Path,
}

/// Check the keys of the prompt (typed batch members, or the statements of a plain brief) and of
/// the card against the live runs of `state`. Returns the admitted keys, to be written into the
/// card that registers the run.
pub fn admit_launch(request: &LaunchRequest) -> Result<Vec<String>, Refusal> {
    admit(
        request.prompt,
        request.extra,
        &Observed {
            state: request.state.to_owned(),
        },
    )
}
