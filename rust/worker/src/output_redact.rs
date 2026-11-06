//! S1 helpers; S3 wires `question` in attempt.rs and `history_text` in
//! attempt.rs and attempt_result_helpers.rs to history/projection writes.
//! History keeps the tail; question fields keep the head, after redaction.
pub fn history_text(text: &str) -> String {
    // redacted_tail redacts the entire input BEFORE applying its bounds:
    // first max_lines, then max_bytes (UTF-8 safe, no truncation marker).
    pg_worker_core::redact::redacted_tail(
        text,
        pg_worker_core::redact::MAX_TAIL_BYTES,
        pg_worker_core::redact::MAX_TAIL_BYTES,
    )
}
pub fn question(mut q: crate::question::Question) -> crate::question::Question {
    q.text = question_text(&q.text);
    q.default = q.default.map(|s| question_text(&s));
    q.risk_tags = q.risk_tags.iter().map(|s| question_text(s)).collect();
    q
}

fn question_text(text: &str) -> String {
    // Redact before keeping the first MAX_TAIL_BYTES bytes, without a marker.
    let redacted = pg_worker_core::redact::redact(text);
    let mut end = redacted.len().min(pg_worker_core::redact::MAX_TAIL_BYTES);
    while !redacted.is_char_boundary(end) {
        end -= 1;
    }
    redacted[..end].to_string()
}

#[cfg(test)]
#[path = "output_redact_tests.rs"]
mod tests;
