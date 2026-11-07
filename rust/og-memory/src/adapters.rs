//! Shared typed host boundary: validation and attribution belong only to Memory.
use crate::{
    io::{Memory, Transport},
    Capabilities, Envelope,
};
use serde::Deserialize;
use serde_json::{json, Value};

#[derive(Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Scope {
    Project,
    Global,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Input {
    pub scope: Scope,
    pub project: Option<String>,
    pub envelope: Option<Envelope>,
    pub query: Option<String>,
}
pub fn dispatch<T: Transport>(
    m: &mut Memory<T>,
    c: &Capabilities,
    op: &str,
    input: Input,
) -> Result<Value, String> {
    let project = match (input.scope, input.project) {
        (Scope::Global, None) => "global".into(),
        (Scope::Project, Some(p)) if p != "global" => p,
        _ => return Err("InvalidScope".into()),
    };
    match op {
        "remember" => {
            let e = input.envelope.ok_or("MissingEnvelope")?;
            if input.query.is_some() {
                return Err("InvalidInput".into());
            }
            m.remember(&project, &e, c)
                .map(|r| json!({"receipt":r.0}))
                .map_err(|e| format!("{e:?}"))
        }
        "recall" if input.envelope.is_none() => {
            let r = m.recall(&project).map_err(|e| format!("{e:?}"))?;
            let saved: Vec<_> = r
                .saved
                .into_iter()
                .filter(|e| {
                    input
                        .query
                        .as_ref()
                        .is_none_or(|q| e.content.contains(q) || e.slug.contains(q))
                })
                .collect();
            let blocked: Vec<_> = r
                .retained_blocked
                .iter()
                .map(|b| format!("{:?}", b.reason))
                .collect();
            Ok(json!({"saved":saved,"retained_blocked":blocked,"stale":r.stale}))
        }
        _ => Err("InvalidOperation".into()),
    }
}
pub fn cli<T: Transport>(
    m: &mut Memory<T>,
    c: &Capabilities,
    args: &[String],
    body: &str,
) -> Result<Value, String> {
    let op = args.first().ok_or("MissingOperation")?;
    let mut fields = serde_json::Map::new();
    let (pairs, remainder) = args[1..].as_chunks::<2>();
    for pair in pairs {
        let key = match pair[0].as_str() {
            "--scope" => "scope",
            "--project" => "project",
            "--query" => "query",
            _ => return Err("InvalidFlag".into()),
        };
        if fields.insert(key.into(), json!(pair[1])).is_some() {
            return Err("DuplicateFlag".into());
        }
    }
    if !remainder.is_empty() {
        return Err("MissingFlagValue".into());
    }
    if op == "remember" {
        let e: Envelope = serde_json::from_str(body).map_err(|_| "InvalidEnvelope")?;
        fields.insert("envelope".into(), json!(e));
    }
    let input = serde_json::from_value(Value::Object(fields)).map_err(|_| "InvalidInput")?;
    dispatch(m, c, op, input)
}
pub fn exit_code(result: &Result<Value, String>) -> i32 {
    i32::from(result.is_err())
}

/// Newline-delimited stdio JSON-RPC. Dynamic parse errors never echo input.
pub fn mcp<T: Transport>(m: &mut Memory<T>, c: &Capabilities, request: Value) -> Option<Value> {
    let id = request.get("id")?.clone();
    let result = match request["method"].as_str() {
        Some("initialize") => {
            json!({"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"og-memory","version":"0.1.0"}})
        }
        Some("tools/list") => {
            json!({"tools":(["memory_remember","memory_recall"].map(|name| json!({"name":name,"inputSchema":{"type":"object","properties":{"scope":{"enum":["global","project"]},"project":{"type":"string"},"envelope":{"type":"object"},"query":{"type":"string"}},"required":["scope"]}})))})
        }
        Some("tools/call") => {
            let p = &request["params"];
            let op = match p["name"].as_str() {
                Some("memory_remember") => "remember",
                Some("memory_recall") => "recall",
                _ => "invalid",
            };
            let output = serde_json::from_value(p["arguments"].clone())
                .map_err(|_| "InvalidInput".into())
                .and_then(|i| dispatch(m, c, op, i));
            json!({"isError":output.is_err(),"content":[{"type":"text","text":serde_json::to_string(&output).unwrap()}]})
        }
        _ => {
            return Some(
                json!({"jsonrpc":"2.0","id":id,"error":{"code":-32601,"message":"Unsupported method"}}),
            )
        }
    };
    Some(json!({"jsonrpc":"2.0","id":id,"result":result}))
}
#[cfg(test)]
mod tests {
    use super::*;
    use crate::io::MockTransport;
    fn caps() -> Capabilities {
        Capabilities::from_schema(include_str!("../tests/fixtures/memory.pg"))
    }
    fn args(op: &str) -> Vec<String> {
        [op, "--scope", "global"].map(String::from).to_vec()
    }
    #[test]
    fn cli_exit_codes_success_outage_recall_outage_refusal_unverified() {
        let mut m = Memory::new(MockTransport::default());
        let mut e = crate::tests::fact();
        let body = serde_json::to_string(&e).unwrap();
        assert_eq!(
            exit_code(&cli(&mut m, &caps(), &args("remember"), &body)),
            0
        );
        m.transport.outage = true;
        assert_eq!(
            exit_code(&cli(&mut m, &caps(), &args("remember"), &body)),
            1
        );
        assert_eq!(exit_code(&cli(&mut m, &caps(), &args("recall"), "")), 1);
        m.transport.outage = false;
        e.slug = "routing-denied".into();
        assert_eq!(
            exit_code(&cli(
                &mut m,
                &caps(),
                &args("remember"),
                &serde_json::to_string(&e).unwrap()
            )),
            1
        );
        e.slug = "unverified".into();
        m.transport.omit_write = true;
        assert_eq!(
            exit_code(&cli(
                &mut m,
                &caps(),
                &args("remember"),
                &serde_json::to_string(&e).unwrap()
            )),
            1
        );
    }
    #[test]
    fn mcp_cli_same_result_and_separate_blocked_ledger() {
        let mut a = Memory::new(MockTransport::default());
        let mut b = Memory::new(MockTransport::default());
        let e = crate::tests::fact();
        let expected = cli(
            &mut a,
            &caps(),
            &args("remember"),
            &serde_json::to_string(&e).unwrap(),
        );
        let response = mcp(&mut b, &caps(), json!({"id":1,"method":"tools/call","params":{"name":"memory_remember","arguments":{"scope":"global","envelope":e}}})).unwrap();
        assert_eq!(
            response["result"]["content"][0]["text"],
            serde_json::to_string(&expected).unwrap()
        );
        b.writes_enabled = false;
        assert!(cli(
            &mut b,
            &caps(),
            &args("remember"),
            &serde_json::to_string(&e).unwrap()
        )
        .is_err());
        let r = cli(&mut b, &caps(), &args("recall"), "").unwrap();
        assert_eq!(r["saved"].as_array().unwrap().len(), 1);
        assert_eq!(r["retained_blocked"], json!(["WritesDisabled"]));
    }
}
