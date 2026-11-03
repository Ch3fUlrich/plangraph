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

pub(crate) const NOT_IMPLEMENTED_MSG: &str = "not implemented in pg-spawnd yet (R6a-3)";

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

fn health_json() -> String {
    format!(
        concat!(
            r#"{{"status":"healthy","daemon":"pg-spawnd","#,
            r#""spawner_version":"pg-spawnd {version}","placement":"off","#,
            r#""runner_jobs_total":0,"active_jobs_count":0,"#,
            r#""catalog_models_count":0,"gateway_default_url":""}}"#
        ),
        version = env!("CARGO_PKG_VERSION")
    )
}

/// Dispatch one `tools/call` request.
///
/// Unknown tool names are a JSON-RPC protocol error that reveals nothing about
/// the daemon internals; listed-but-unbuilt tools return an MCP tool error.
pub(crate) fn call_tool(request: &CallToolRequestParams) -> Result<CallToolResult, McpError> {
    let name = request.name.as_ref();
    match name {
        "plangraph_agent_health" => Ok(CallToolResult::success(vec![ContentBlock::text(
            health_json(),
        )])),
        other if tool_names().contains(other) => {
            Ok(CallToolResult::error(vec![ContentBlock::text(
                NOT_IMPLEMENTED_MSG,
            )]))
        }
        _ => Err(McpError::new(
            ErrorCode::INVALID_PARAMS,
            format!("unknown tool: {name}"),
            None,
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(name: &str) -> CallToolRequestParams {
        serde_json::from_value(serde_json::json!({ "name": name })).expect("request")
    }

    #[test]
    fn lists_the_embedded_fixture() {
        let listed = list_tools().tools;
        let fixture: serde_json::Value = serde_json::from_str(TOOLS_JSON).expect("fixture");
        assert_eq!(serde_json::to_value(&listed).expect("tools"), fixture);
        assert_eq!(listed.len(), 15);
    }

    #[test]
    fn tool_names_are_cached() {
        assert!(std::ptr::eq(tool_names(), tool_names()));
        assert_eq!(tool_names().len(), tools().len());
    }

    #[test]
    fn health_payload_shape() {
        let result = call_tool(&request("plangraph_agent_health")).expect("health");
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
        let result = call_tool(&request("plangraph_session_list")).expect("tool error");
        assert_eq!(result.is_error, Some(true));
        let text = result.content[0].as_text().expect("text").text.clone();
        assert!(text.contains("not implemented in pg-spawnd yet (R6a-3)"));
    }

    #[test]
    fn unknown_tool_is_protocol_error_naming_no_internals() {
        let error = call_tool(&request("no_such_tool")).expect_err("unknown tool");
        assert_eq!(error.code, ErrorCode::INVALID_PARAMS);
        assert_eq!(error.message, "unknown tool: no_such_tool");
    }
}
