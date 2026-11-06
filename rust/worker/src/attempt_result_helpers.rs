/// A cancelled attempt: keep the branch and head the caller already knows and
/// skip the commit, confinement and check stages. `output_tail` carries what
/// the harness printed before it was killed.
fn cancelled_result(branch: &str, head: &str, output_tail: String) -> AttemptResult {
    AttemptResult {
        exit_class: ExitClass::Cancelled,
        diff_hash: String::new(),
        check_hash: String::new(),
        check_tail: String::new(),
        context_overflow: false,
        branch: branch.to_string(),
        head: head.to_string(),
        output_tail: history_text(&output_tail),
        harness: None,
        model: None,
        combo: None,
        class: None,
        started_at: None,
        ended_at: None,
        tokens_in: None,
        tokens_out: None,
        cost: None,
        log_ref: None,
        question: None,
    }
}

/// A contract-defect result, keeping the diff so the planner can still tell
/// whether anything was written.
fn defect_result(
    dest: &Path,
    base: &str,
    branch: &str,
    head: &str,
    output_tail: String,
) -> Result<AttemptResult, AttemptError> {
    Ok(AttemptResult {
        exit_class: ExitClass::ContractDefect,
        diff_hash: confine::diff_hash(dest, base)?,
        check_hash: String::new(),
        check_tail: String::new(),
        context_overflow: false,
        branch: branch.to_string(),
        head: head.to_string(),
        output_tail: history_text(&output_tail),
        harness: None,
        model: None,
        combo: None,
        class: None,
        started_at: None,
        ended_at: None,
        tokens_in: None,
        tokens_out: None,
        cost: None,
        log_ref: None,
        question: None,
    })
}
