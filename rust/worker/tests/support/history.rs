/// Every payload in a history, base64-decoded: activity inputs and results
/// are encoded in the JSON history, so raw substring searches cannot see them.
pub fn decoded_payloads(v: &serde_json::Value) -> Vec<String> {
    match v {
        serde_json::Value::Object(map) => {
            let mut out = Vec::new();
            for (k, x) in map {
                if k == "payloads"
                    && let Some(items) = x.as_array()
                {
                    for p in items {
                        if let Some(data) = p.get("data").and_then(|d| d.as_str()) {
                            out.push(
                                String::from_utf8_lossy(&super::b64_decode(data)).into_owned(),
                            );
                        }
                    }
                }
                out.extend(decoded_payloads(x));
            }
            out
        }
        serde_json::Value::Array(a) => a.iter().flat_map(decoded_payloads).collect(),
        _ => Vec::new(),
    }
}
