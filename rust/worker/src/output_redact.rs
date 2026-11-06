pub fn history_text(text: &str) -> String {
    pg_worker_core::redact::redacted_tail(text, usize::MAX, pg_worker_core::redact::MAX_TAIL_BYTES)
}
pub fn question(mut q: crate::question::Question) -> crate::question::Question {
    q.text = history_text(&q.text);
    q.default = q.default.map(|s| history_text(&s));
    q.risk_tags = q.risk_tags.iter().map(|s| history_text(s)).collect();
    q
}

#[cfg(test)]
#[path = "output_redact_tests.rs"]
mod tests;
