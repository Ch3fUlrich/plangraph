//! Timer entrypoint. No network credentials or model invocation in this process.
#[path = "../code_dispatch.rs"]
mod code_dispatch;

use code_dispatch::{tick, Backend};
use serde_json::{json, Value};
use std::io::Write;
use std::process::{Command, Stdio};

struct Bridge(String);
impl Backend for Bridge {
    fn call(&mut self, verb: &str, request: Value) -> Result<Value, String> {
        let mut child = Command::new(&self.0)
            .arg(verb)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|_| "bridge unavailable")?;
        child
            .stdin
            .take()
            .ok_or("bridge stdin unavailable")?
            .write_all(
                serde_json::to_string(&request)
                    .map_err(|_| "invalid request")?
                    .as_bytes(),
            )
            .map_err(|_| "bridge write failed")?;
        let output = child.wait_with_output().map_err(|_| "bridge wait failed")?;
        if !output.status.success() {
            return Err("bridge refused".into());
        }
        serde_json::from_slice(&output.stdout).map_err(|_| "invalid bridge response".into())
    }
}
fn main() {
    if let Err(error) = run() {
        eprintln!("code-dispatch: {error}");
        std::process::exit(1);
    }
}
fn run() -> Result<(), String> {
    let path = std::env::var("PG_CODE_DISPATCH_BRIDGE")
        .map_err(|_| "PG_CODE_DISPATCH_BRIDGE must name a trusted executable")?;
    if !std::path::Path::new(&path).is_absolute() {
        return Err("bridge executable must be absolute".into());
    }
    let mut bridge = Bridge(path);
    // Bridge holds a cross-host fenced dispatcher lock through the entire tick.
    let snapshot = bridge.call("begin", json!({"principal":"code-dispatcher"}))?;
    let result = (|| {
        let items = serde_json::from_value(
            snapshot
                .get("items")
                .cloned()
                .ok_or("missing ready items")?,
        )
        .map_err(|_| "invalid ready items")?;
        let routes = serde_json::from_value(
            snapshot
                .get("routes")
                .cloned()
                .ok_or("missing route capacity")?,
        )
        .map_err(|_| "invalid route capacity")?;
        tick(&mut bridge, items, routes)
    })();
    let end = bridge.call("end", json!({"principal":"code-dispatcher"}));
    let report = result?;
    end?;
    println!(
        "{}",
        serde_json::to_string(&report).map_err(|_| "invalid report")?
    );
    Ok(())
}
