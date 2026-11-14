//! The bridge reports the same effective URL and credential check used for launch.
use serde_json::{json, Value};
use std::{path::Path, time::Duration};

fn decode(result: Option<&[u8]>) -> Value {
    result
        .and_then(|bytes| serde_json::from_slice::<Value>(bytes).ok())
        .filter(|v| {
            v["gateway_default_url"].is_string()
                && v.get("gateway_error")
                    .is_some_and(|e| e.is_null() || e.is_string())
        })
        .unwrap_or_else(|| {
            json!({"gateway_default_url":"",
            "gateway_error":"gateway not configured: health bridge unavailable"})
        })
}
pub(crate) fn read(state: &Path) -> Value {
    let result =
        crate::adapter::Adapter(state).run(&["gateway-health".into()], Duration::from_secs(5));
    match result {
        Ok((0, bytes, _)) => decode(Some(&bytes)),
        _ => decode(None),
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn missing_bridge_is_a_named_configuration_failure() {
        assert!(decode(None)["gateway_error"]
            .as_str()
            .unwrap()
            .starts_with("gateway not configured"));
        let response =
            br#"{"gateway_default_url":"https://gateway.example.invalid/v1","gateway_error":null}"#;
        assert!(decode(Some(response))["gateway_error"].is_null());
        assert!(decode(Some(b"{}"))["gateway_error"].is_string());
    }
}
