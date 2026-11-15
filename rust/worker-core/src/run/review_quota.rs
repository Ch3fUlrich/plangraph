//! Provider-shaped quota refusals; ordinary review prose cannot stop a run.
use regex::Regex;
use std::sync::LazyLock;
static QUOTA: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
    r"(?i)^\s*(?:(?:error:\s*)?(?:RESOURCE_EXHAUSTED(?:\s*\(code \d{3}\):|:|\s*$)|HTTP 429\b)|(?:error:\s*)?(?:individual )?quota (?:reached|exceeded)|error:.*(?:rate limit|too many requests|quota (?:exhausted|refus)))"
).unwrap()
});

pub fn reason(stdout: &str, stderr: &str) -> Option<String> {
    for line in stderr.split(['\r', '\n']).chain(stdout.split(['\r', '\n'])) {
        let text = super::activity::strip_ansi(line);
        if QUOTA.is_match(&text) {
            return Some(format!("review_quota: {}", text.trim()));
        }
        if let Ok(event) = serde_json::from_str::<serde_json::Value>(&text) {
            let response = event
                .get("autoos_provider_response")
                .or_else(|| (event["type"] == "error").then(|| &event["error"]));
            if let Some(response) = response.filter(|r| r["status"] == 429) {
                return Some(format!(
                    "review_quota: HTTP 429: {}",
                    response
                        .get("body")
                        .or_else(|| response.get("message"))
                        .unwrap_or(&serde_json::Value::Null)
                ));
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn provider_errors_are_evidence_but_review_prose_is_not() {
        for text in [
            "RESOURCE_EXHAUSTED (code 429): Individual quota reached",
            "RESOURCE_EXHAUSTED: quota exhausted",
            "spinner\rRESOURCE_EXHAUSTED (code 429): Individual quota reached",
            "HTTP 429: Too Many Requests",
            "quota reached, resets in 46m",
            "Error: provider quota refusal",
            r#"{"autoos_provider_response":{"status":429,"body":"quota"}}"#,
            r#"{"type":"error","error":{"status":429,"message":"quota"}}"#,
        ] {
            assert!(reason(text, "").is_some(), "{text}");
            assert!(reason("", text).is_some(), "{text}");
        }
        for text in [
            "I tested HTTP 429: quota reached",
            "The RESOURCE_EXHAUSTED handling now works",
            r#"{"type":"assistant","message":"HTTP 429"}"#,
            "VERDICT: ACCEPT",
            "resource_exhausted was handled; rate limit resets in 5m",
        ] {
            assert!(reason(text, text).is_none(), "{text}");
        }
    }
}
