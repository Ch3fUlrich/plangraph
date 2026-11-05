use crate::activity::Kind;
use serde_json::Value;

pub(super) fn gate(event: &Value) -> Kind {
    let info = event
        .get("rate_limit_info")
        .filter(|v| v.is_object())
        .unwrap_or(event);
    let denied = info["status"].as_str().is_some_and(|s| {
        matches!(
            s.to_ascii_lowercase().as_str(),
            "rejected" | "denied" | "blocked" | "exceeded" | "limit_reached"
        )
    });
    let quota = [
        "message",
        "text",
        "error",
        "detail",
        "reason",
        "description",
    ]
    .iter()
    .any(|key| {
        info[*key].as_str().is_some_and(|s| {
            let lower = s.to_ascii_lowercase();
            ["usage", "credit", "daily"]
                .iter()
                .any(|w| lower.contains(w))
        })
    });
    if denied || quota {
        Kind::Error
    } else {
        Kind::Request
    }
}
