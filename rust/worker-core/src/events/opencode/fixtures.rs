// Historical projections: types, statuses, tools and text presence preserved.
// IDs, paths, call IDs and unused metadata removed; timestamps are synthetic.
// Source: worker/tests/fixtures/opencode; empty is 2.1.1, others 2.0.32.
pub const QUOTA: &str = r#"{"type":"step_start","timestamp":1,"part":{}}
{"type":"error","timestamp":2,"error":{"type":"provider.quota","status":429,"message":"FreeUsageLimitError"}}
"#;
pub const DEEPSEEK: &str = r#"{"type":"step_start","timestamp":1,"part":{}}
{"type":"error","timestamp":2,"error":{"type":"provider.quota","status":429,"message":"FreeUsageLimitError"}}
"#;
pub const TRANSPORT: &str = r#"{"type":"step_start","timestamp":1,"part":{}}
{"type":"error","timestamp":2,"error":{"type":"unknown","message":"Transport"}}
"#;
pub const TEXT: &str = r#"{"type":"step_start","timestamp":1,"part":{}}
{"type":"text","timestamp":2,"part":{"text":"OK"}}
"#;
pub const TOOL: &str = r#"{"type":"step_start","timestamp":1,"part":{}}
{"type":"tool_use","timestamp":2,"part":{"tool":"write","state":{"status":"completed"}}}
{"type":"step_finish","timestamp":3,"part":{}}
{"type":"step_start","timestamp":4,"part":{}}
{"type":"text","timestamp":5,"part":{"text":"DONE"}}
"#;
pub const DENIED: &str = r#"{"type":"step_start","timestamp":1,"part":{}}
{"type":"tool_use","timestamp":2,"part":{"tool":"write","state":{"status":"error"}}}
{"type":"tool_use","timestamp":3,"part":{"tool":"write","state":{"status":"completed"}}}
{"type":"step_finish","timestamp":4,"part":{}}
{"type":"step_start","timestamp":5,"part":{}}
{"type":"text","timestamp":6,"part":{"text":"Results"}}
"#;
pub const EMPTY: &str = r#"{"type":"step_start","timestamp":1,"part":{}}
"#;

pub const CAPTURE_VERSION: &str = "2.1.0";
pub const ARRIVAL_MS: &[u64] = &[
    4001, 4167, 4219, 5981, 6266, 10546, 10614, 11807, 12315, 12323, 23171, 23225, 24657, 24972,
    25027, 26573, 26573,
];
pub const CLOSED_STDIN: &str = r#"{"type":"step_start","part":{}}
{"type":"tool_use","part":{"tool":"read","state":{"status":"error"}}}
{"type":"step_finish","part":{}}
{"type":"step_start","part":{}}
{"type":"tool_use","part":{"tool":"read","state":{"status":"completed"}}}
{"type":"tool_use","part":{"tool":"glob","state":{"status":"completed"}}}
{"type":"step_finish","part":{}}
{"type":"step_start","part":{}}
{"type":"tool_use","part":{"tool":"read","state":{"status":"error"}}}
{"type":"tool_use","part":{"tool":"read","state":{"status":"error"}}}
{"type":"tool_use","part":{"tool":"shell","state":{"status":"completed"}}}
{"type":"step_finish","part":{}}
{"type":"step_start","part":{}}
{"type":"tool_use","part":{"tool":"read","state":{"status":"completed"}}}
{"type":"step_finish","part":{}}
{"type":"step_start","part":{}}
{"type":"text","part":{"text":"OK"}}
"#;
