//! MCP tool catalogue and dispatch for `pg-spawnd`.
//!
//! `tools_list.json` is the committed `TOOLS_METADATA` fixture; `tools/list`
//! serves it verbatim so the Rust and Python servers share one contract.

use rmcp::model::{
    CallToolRequestParams, CallToolResult, ContentBlock, ErrorCode, ListToolsResult, Tool,
};
use rmcp::ErrorData as McpError;
use std::collections::HashSet;
use std::sync::OnceLock;

/// Committed copy of `plangraph.spawner.mcp.TOOLS_METADATA` (regenerate via the
/// command in the R6a-1 brief when the Python metadata changes).
pub(crate) const TOOLS_JSON: &str = include_str!("tools_list.json");

pub(crate) fn tools() -> Vec<Tool> {
    static TOOLS: OnceLock<Vec<Tool>> = OnceLock::new();
    TOOLS
        .get_or_init(|| {
            serde_json::from_str(TOOLS_JSON).expect("tools_list.json must be a tool array")
        })
        .clone()
}

fn tool_names() -> &'static HashSet<String> {
    static NAMES: OnceLock<HashSet<String>> = OnceLock::new();
    NAMES.get_or_init(|| {
        tools()
            .into_iter()
            .map(|tool| tool.name.into_owned())
            .collect()
    })
}

pub(crate) fn list_tools() -> ListToolsResult {
    ListToolsResult::with_all_items(tools())
}

fn health_json(total: u64, running: usize) -> String {
    format!(
        concat!(
            r#"{{"status":"healthy","daemon":"pg-spawnd","#,
            r#""spawner_version":"pg-spawnd {version}","placement":"off","#,
            r#""runner_jobs_total":{total},"active_jobs_count":{running},"#,
            r#""catalog_models_count":0,"gateway_default_url":""}}"#
        ),
        version = env!("CARGO_PKG_VERSION"),
        total = total,
        running = running
    )
}

/// Dispatch one `tools/call` request.
///
/// Unknown tool names are a JSON-RPC protocol error that reveals nothing about
/// the daemon internals; listed-but-unbuilt tools return an MCP tool error.
#[cfg(test)]
pub(crate) fn call_tool(
    request: &CallToolRequestParams,
    state: &std::path::Path,
) -> Result<CallToolResult, McpError> {
    call_tool_with_host(request, state, "local")
}

#[cfg(test)]
pub(crate) fn call_tool_with_host(
    request: &CallToolRequestParams,
    state: &std::path::Path,
    host: &str,
) -> Result<CallToolResult, McpError> {
    dispatch(request, state, host, None)
}
pub(crate) fn dispatch(
    request: &CallToolRequestParams,
    state: &std::path::Path,
    host: &str,
    runs: Option<&crate::runs::Runs>,
) -> Result<CallToolResult, McpError> {
    let name = request.name.as_ref();
    if name.starts_with("plangraph_package_") {
        let payload =
            crate::package::handle(state, name, &request.arguments.clone().unwrap_or_default())
                .map_err(crate::errors::ToolError::into_error_data)?;
        return Ok(CallToolResult::success(vec![ContentBlock::text(
            payload.to_string(),
        )]));
    }
    if matches!(
        name,
        "plangraph_agent_cancel" | "plangraph_agent_steer" | "plangraph_agent_status"
    ) && !(name == "plangraph_agent_status"
        && request
            .arguments
            .as_ref()
            .is_some_and(|a| a.contains_key("run_id")))
    {
        if let Some(runs) = runs {
            let payload = runs
                .job_tool(state, name, &request.arguments.clone().unwrap_or_default())
                .map_err(crate::errors::ToolError::into_error_data)?;
            if let Some(error) = crate::errors::refused_record_error(&payload) {
                return Err(error);
            }
            return Ok(CallToolResult::success(vec![ContentBlock::text(
                serde_json::to_string_pretty(&payload).expect("JSON value"),
            )]));
        }
        return Err(
            crate::errors::ToolError::Internal("no run registry attached".into()).into_error_data(),
        );
    }
    if matches!(
        name,
        "plangraph_agent_route" | "plangraph_agent_probe" | "plangraph_agent_spawn"
    ) {
        let args = request.arguments.clone().unwrap_or_default();
        let payload = match name {
            "plangraph_agent_spawn" => {
                let runs = runs.ok_or_else(|| {
                    crate::errors::ToolError::Internal("supervisor unavailable".into())
                        .into_error_data()
                })?;
                let record = crate::spawn::spawn(state, &args, host, runs)
                    .map_err(crate::errors::ToolError::into_error_data)?;
                if let Some(error) = crate::errors::refused_record_error(&record) {
                    return Err(error);
                }
                serde_json::to_string_pretty(&record).map_err(|_| {
                    crate::errors::ToolError::Internal("could not encode run record".into())
                })
            }
            "plangraph_agent_probe" => crate::placement::probe(state, &args),
            _ => crate::placement::route(state, &args),
        }
        .map_err(crate::errors::ToolError::into_error_data)?;
        return Ok(CallToolResult::success(vec![ContentBlock::text(payload)]));
    }
    if matches!(
        name,
        "plangraph_agent_ps"
            | "plangraph_agent_result"
            | "plangraph_agent_status"
            | "plangraph_agent_steer"
    ) {
        let args = request.arguments.clone().unwrap_or_default();
        let required = |key: &str| {
            args.get(key)
                .ok_or_else(|| crate::errors::ToolError::Key(key.into()))
        };
        let payload = (|| {
            use serde_json::json;
            match name {
                "plangraph_agent_ps" => {
                    let mut runs = crate::records::list_runs(state);
                    runs.extend(
                        crate::package::list(state)
                            .into_iter()
                            .map(crate::package::view),
                    );
                    Ok(json!({"runs": runs}))
                }
                "plangraph_agent_result" => crate::records::read_record(state, required("run_id")?),
                "plangraph_agent_status" if args.contains_key("run_id") => {
                    crate::records::read_record(state, required("run_id")?)
                }
                "plangraph_agent_status" => {
                    if let Some(id) = args.get("job_id") {
                        return Err(crate::errors::ToolError::Key(format!(
                            "Job {} not found",
                            python_str(id)
                        )));
                    }
                    Ok(json!({"jobs":[]}))
                }
                _ => {
                    let _job = python_str(required("job_id")?);
                    let _message = python_str(required("message")?);
                    Ok(json!({"ok":false,"steered":false,"error":"unknown job"}))
                }
            }
        })()
        .map_err(crate::errors::ToolError::into_error_data)?;
        if let Some(error) = crate::errors::refused_record_error(&payload) {
            return Err(error);
        }
        return Ok(CallToolResult::success(vec![ContentBlock::text(
            serde_json::to_string_pretty(&payload)
                .map_err(|e| crate::errors::ToolError::Internal(e.to_string()).into_error_data())?,
        )]));
    }
    match name {
        "plangraph_agent_health" => Ok(CallToolResult::success(vec![ContentBlock::text({
            let (total, running) = runs.map(|r| r.health_counts(state)).unwrap_or_default();
            health_json(total, running)
        })])),
        other if tool_names().contains(other) => {
            Ok(CallToolResult::error(vec![ContentBlock::text(format!(
                "not implemented in pg-spawnd yet ({})",
                match other {
                    "plangraph_agent_query_catalog" => "R11",
                    _ => "R9",
                }
            ))]))
        }
        _ => Err(McpError::new(
            ErrorCode::INVALID_PARAMS,
            format!("unknown tool: {name}"),
            None,
        )),
    }
}

pub(crate) fn python_str(value: &serde_json::Value) -> String {
    // Known deviation: integer tokens beyond u64/i64 become f64, losing digits.
    // Keep this behavior rather than enabling workspace-wide arbitrary_precision.
    match value {
        serde_json::Value::String(s) => s.clone(),
        serde_json::Value::Null => "None".into(),
        serde_json::Value::Bool(true) => "True".into(),
        serde_json::Value::Bool(false) => "False".into(),
        serde_json::Value::Array(values) => format!(
            "[{}]",
            values
                .iter()
                .map(python_repr)
                .collect::<Vec<_>>()
                .join(", ")
        ),
        serde_json::Value::Object(values) => format!(
            "{{{}}}",
            values
                .iter()
                .map(|(key, value)| format!(
                    "{}: {}",
                    python_repr(&serde_json::Value::String(key.clone())),
                    python_repr(value)
                ))
                .collect::<Vec<_>>()
                .join(", ")
        ),
        serde_json::Value::Number(n) if n.is_f64() => python_float(n.as_f64().unwrap()),
        _ => value.to_string(),
    }
}

fn python_float(value: f64) -> String {
    // Rust's shortest round-trip formatter supplies the digits; Python chooses
    // scientific notation at different thresholds and pads signed exponents.
    let scientific = format!("{value:e}");
    let (mantissa, exponent) = scientific.split_once('e').unwrap();
    let exponent: i32 = exponent.parse().unwrap();
    if !(-4..16).contains(&exponent) {
        format!("{mantissa}e{exponent:+03}")
    } else {
        let mut decimal = value.to_string();
        if !decimal.contains('.') {
            decimal.push_str(".0");
        }
        decimal
    }
}

fn python_repr(value: &serde_json::Value) -> String {
    if let serde_json::Value::String(text) = value {
        let quote = if text.contains('\'') && !text.contains('"') {
            '"'
        } else {
            '\''
        };
        let escaped = text
            .replace('\\', "\\\\")
            .replace('\n', "\\n")
            .replace('\r', "\\r")
            .replace('\t', "\\t")
            .replace(quote, &format!("\\{quote}"));
        format!("{quote}{escaped}{quote}")
    } else {
        python_str(value)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn job_tools_require_registry() {
        for verb in ["cancel", "steer", "status"] {
            let request: CallToolRequestParams = serde_json::from_value(serde_json::json!({
                "name":format!("plangraph_agent_{verb}"), "arguments":{}
            }))
            .unwrap();
            let error = dispatch(&request, std::path::Path::new("."), "local", None).unwrap_err();
            assert_eq!(error.message, "no run registry attached");
            assert_eq!(error.code, rmcp::model::ErrorCode::INTERNAL_ERROR);
        }
    }

    #[test]
    fn python_float_coercion() {
        let large = serde_json::from_str("123456789012345678901234567890").unwrap();
        assert_eq!(python_str(&large), "1.2345678901234568e+29");
        for (value, expected) in [
            (1e30, "1e+30"),
            (1.5, "1.5"),
            (1e-5, "1e-05"),
            (0.0001, "0.0001"),
            (1e16, "1e+16"),
            (123.0, "123.0"),
            (-0.5, "-0.5"),
            (12345678901234567.0, "1.2345678901234568e+16"),
        ] {
            assert_eq!(python_str(&serde_json::json!(value)), expected);
        }
    }

    #[test]
    fn python_string_coercion() {
        for (value, expected) in [
            (serde_json::json!(12), "12"),
            (serde_json::json!(true), "True"),
            (serde_json::json!(false), "False"),
            (serde_json::json!(null), "None"),
            (serde_json::json!("text"), "text"),
            (
                serde_json::json!(["text", null, true]),
                "['text', None, True]",
            ),
            (serde_json::json!({"a":false}), "{'a': False}"),
        ] {
            assert_eq!(python_str(&value), expected);
        }
    }

    fn request(name: &str) -> CallToolRequestParams {
        serde_json::from_value(serde_json::json!({ "name": name })).expect("request")
    }

    #[test]
    fn lists_the_embedded_fixture() {
        let listed = list_tools().tools;
        let fixture: serde_json::Value = serde_json::from_str(TOOLS_JSON).expect("fixture");
        assert_eq!(serde_json::to_value(&listed).expect("tools"), fixture);
        assert_eq!(listed.len(), 20);
    }

    #[test]
    fn tool_names_are_cached() {
        assert!(std::ptr::eq(tool_names(), tool_names()));
        assert_eq!(tool_names().len(), tools().len());
    }

    #[test]
    fn health_payload_shape() {
        let result = call_tool(
            &request("plangraph_agent_health"),
            std::path::Path::new("unused"),
        )
        .expect("health");
        assert_eq!(result.is_error, Some(false));
        let text = result.content[0].as_text().expect("text").text.clone();
        let payload: serde_json::Value = serde_json::from_str(&text).expect("payload");
        assert_eq!(payload["status"], "healthy");
        assert_eq!(payload["daemon"], "pg-spawnd");
        assert_eq!(
            payload["spawner_version"],
            format!("pg-spawnd {}", env!("CARGO_PKG_VERSION"))
        );
        assert_eq!(payload["placement"], "off");
        for key in [
            "runner_jobs_total",
            "active_jobs_count",
            "catalog_models_count",
        ] {
            assert_eq!(payload[key], 0, "{key}");
        }
        assert_eq!(payload["gateway_default_url"], "");
    }

    #[test]
    fn not_implemented_is_tool_error_naming_r6a_3() {
        let result = call_tool(
            &request("plangraph_session_list"),
            std::path::Path::new("unused"),
        )
        .expect("tool error");
        assert_eq!(result.is_error, Some(true));
        let text = result.content[0].as_text().expect("text").text.clone();
        assert!(text.contains("not implemented in pg-spawnd yet (R9)"));
    }

    #[test]
    fn unknown_tool_is_protocol_error_naming_no_internals() {
        let error = call_tool(&request("no_such_tool"), std::path::Path::new("unused"))
            .expect_err("unknown tool");
        assert_eq!(error.code, ErrorCode::INVALID_PARAMS);
        assert_eq!(error.message, "unknown tool: no_such_tool");
    }
}
