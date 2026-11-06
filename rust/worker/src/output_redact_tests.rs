use super::*;

#[test]
fn question_text_default_and_tags_are_redacted() {
    let secret = format!("{}{}", "sk-", "pilotseed-0123456789abcdef");
    let q = crate::question::Question {
        text: secret.clone(),
        default: Some(secret.clone()),
        deadline_s: None,
        risk_tags: vec![secret],
    };
    let json = serde_json::to_string(&question(q)).unwrap();
    assert!(!json.contains("pilotseed"), "{json}");
}
