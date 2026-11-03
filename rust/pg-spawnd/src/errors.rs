//! Python spawner JSON-RPC error compatibility.
use rmcp::{model::ErrorCode, ErrorData};
use serde_json::{json, Value};

#[allow(dead_code)] // Admission and launch callers arrive in the next slices.
pub(crate) enum RefusalKind {
    Parameter,
    Spawn,
    Benchmark,
}
#[allow(dead_code)]
pub(crate) enum ToolError {
    Refused {
        kind: RefusalKind,
        message: String,
        legs: Vec<String>,
    },
    Value(String),
    Key(String),
    Type(String),
    Internal(String),
}
impl ToolError {
    pub(crate) fn into_error_data(self) -> ErrorData {
        let (code, message, data) = match self {
            Self::Refused {
                kind,
                message,
                legs,
            } => {
                let (code, name, exit) = match kind {
                    RefusalKind::Parameter => (-32602, "ParameterRefused", 2),
                    RefusalKind::Spawn => (-32000, "SpawnRefused", 2),
                    RefusalKind::Benchmark => (-32602, "BenchmarkError", 3),
                };
                (
                    code,
                    message,
                    json!({"type":name,"exit_code":exit,"legs":legs}),
                )
            }
            other => {
                let (code, name, message) = match other {
                    Self::Value(s) => (-32602, "ValueError", s),
                    Self::Key(s) => (-32602, "KeyError", format!("'{s}'")),
                    Self::Type(s) => (-32602, "TypeError", s),
                    Self::Internal(s) => (-32603, "RuntimeError", s),
                    Self::Refused { .. } => unreachable!(),
                };
                (code, message, json!({"type":name,"exit_code":1}))
            }
        };
        ErrorData::new(ErrorCode(code), message, Some(data))
    }
}

pub(crate) fn refused_record_error(value: &Value) -> Option<ErrorData> {
    if !value.is_object() || value["status"] != "refused" {
        return None;
    }
    let refusal = value
        .get("refusal")
        .cloned()
        .unwrap_or(json!("SpawnRefused"));
    let code = match refusal.as_str() {
        Some("ParameterRefused" | "BenchRequired" | "BenchDataError") => -32602,
        _ => -32000,
    };
    Some(ErrorData::new(
        ErrorCode(code),
        value["error"]
            .as_str()
            .unwrap_or("benchmark rows missing")
            .to_owned(),
        Some(
            json!({"type":refusal,"exit_code":value.get("exit_code").unwrap_or(&json!(2)),
            "legs":value.get("legs").filter(|v| python_truthy(v)).unwrap_or(&json!([])),
            "run_id":value.get("run_id").filter(|v| python_truthy(v)).unwrap_or(&Value::Null)}),
        ),
    ))
}

fn python_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(v) => *v,
        Value::Number(v) => v.as_f64() != Some(0.0),
        Value::String(v) => !v.is_empty(),
        Value::Array(v) => !v.is_empty(),
        Value::Object(v) => !v.is_empty(),
    }
}
