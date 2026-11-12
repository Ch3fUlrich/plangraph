//! Keeper MCP schema snapshot and local JSON-RPC replies (no HTTP client).
mod python_text;
use python_text::{python_float, python_str};
use serde::Serialize;
use serde_json::{json, ser::Formatter, Value};
use std::io::{self, Write};
use std::sync::OnceLock;

/// The committed Python TOOLS snapshot, embedded without runtime file access.
pub fn tools_list() -> Value {
    static TOOLS: OnceLock<Value> = OnceLock::new();
    TOOLS
        .get_or_init(|| {
            serde_json::from_str(include_str!(
                "../../../tests/fixtures/keeper_mcp_tools.json"
            ))
            .expect("valid keeper MCP tool fixture")
        })
        .clone()
}

// Python serve() uses json.dumps(ensure_ascii=False): insertion order and spaces
// after commas/colons, followed by LF. serde_json's compact formatter differs.
struct PythonFormatter;
impl Formatter for PythonFormatter {
    fn begin_array_value<W: ?Sized + Write>(&mut self, w: &mut W, first: bool) -> io::Result<()> {
        if first {
            Ok(())
        } else {
            w.write_all(b", ")
        }
    }
    fn begin_object_key<W: ?Sized + Write>(&mut self, w: &mut W, first: bool) -> io::Result<()> {
        self.begin_array_value(w, first)
    }
    fn begin_object_value<W: ?Sized + Write>(&mut self, w: &mut W) -> io::Result<()> {
        w.write_all(b": ")
    }
    fn write_f64<W: ?Sized + Write>(&mut self, w: &mut W, value: f64) -> io::Result<()> {
        w.write_all(python_float(value).as_bytes())
    }
}

fn wire(value: Value) -> String {
    let mut bytes = Vec::new();
    value
        .serialize(&mut serde_json::Serializer::with_formatter(
            &mut bytes,
            PythonFormatter,
        ))
        .expect("JSON value serializes into memory");
    bytes.push(b'\n');
    String::from_utf8(bytes).expect("JSON is UTF-8")
}

fn error(id: &Value, code: i32, message: &str) -> String {
    wire(json!({"jsonrpc": "2.0", "id": id,
        "error": {"code": code, "message": message}}))
}

/// A Python-framed JSON-RPC reply, or silence for notifications/null ids.
/// tools/call deliberately fails locally; no credentials, filesystem or network.
pub fn handle(message: &Value) -> Option<String> {
    if !message.is_object() || message.get("method").is_none() {
        return Some(error(&Value::Null, -32600, "invalid request"));
    }
    let method = message["method"].as_str();
    let id = &message["id"];
    if method.is_some_and(|method| method.starts_with("notifications/")) || id.is_null() {
        return None;
    }
    let result = match method {
        Some("initialize") => {
            let version = message["params"]
                .get("protocolVersion")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .unwrap_or("2025-06-18");
            json!({"protocolVersion": version, "capabilities": {"tools": {}},
                "serverInfo": {"name": "plangraph-keeper", "version": "0.1"}})
        }
        Some("tools/list") => json!({"tools": tools_list()}),
        Some("tools/call") => {
            json!({"content": [{"type": "text", "text": "not_implemented: tools/call"}],
            "isError": true})
        }
        Some("ping") => json!({}),
        _ => {
            return Some(error(
                id,
                -32601,
                &format!("method not found: {}", python_str(&message["method"])),
            ))
        }
    };
    Some(wire(json!({"jsonrpc": "2.0", "id": id, "result": result})))
}
