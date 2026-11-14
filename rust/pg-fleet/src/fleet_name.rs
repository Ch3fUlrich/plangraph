use serde_json::Value;
use std::path::PathBuf;

pub fn role_of(name: &str) -> String {
    crate::handoff::re(r"-\d{4,}$")
        .replace(name, "")
        .into_owned()
}
pub fn routing_state() -> PathBuf {
    std::env::var_os("ROUTING_STATE_DIR")
        .filter(|s| !s.is_empty())
        .map_or_else(|| crate::home().join(".local/state/routing"), Into::into)
}
pub fn truth(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::String(s) => !s.is_empty(),
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}
pub fn string<'a>(v: &'a Value, key: &str) -> &'a str {
    v[key].as_str().unwrap_or("")
}

pub fn live_name(role: &str, rows: &[Value]) -> Option<String> {
    rows.iter()
        .filter(|r| r.is_object() && truth(&r["status"]) && role_of(string(r, "name")) == role)
        .max_by(|a, b| {
            a["startedAt"]
                .as_f64()
                .unwrap_or(0.0)
                .total_cmp(&b["startedAt"].as_f64().unwrap_or(0.0))
                .then(string(a, "name").cmp(string(b, "name")))
        })
        .map(|r| string(r, "name").to_owned())
}
pub fn choose(role: &str, local: &Value, remote: &[Value], spec: &Value) -> Option<String> {
    let mut rows: Vec<Value> = local.as_array().map_or_else(Vec::new, |a| {
        a.iter().filter(|r| r.is_object()).cloned().collect()
    });
    rows.extend_from_slice(remote);
    let mut name = live_name(role, &rows);
    if let Some(r) = rows
        .iter()
        .find(|r| r.get("sessionId") == spec.get("session_id") && truth(&r["status"]))
    {
        name = r["name"]
            .as_str()
            .filter(|s| !s.is_empty())
            .or(spec["session_name"].as_str())
            .map(str::to_owned);
    }
    name.filter(|s| !s.is_empty()).or_else(|| {
        spec["session_name"]
            .as_str()
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
    })
}

pub fn probe_remote(
    name: &str,
    mut probe: impl FnMut() -> Result<String, String>,
    mut pause: impl FnMut(),
) -> (Vec<Value>, Vec<String>) {
    for attempt in 1..=2 {
        match probe() {
            Ok(out) => {
                if let Ok(Value::Array(a)) = serde_json::from_str::<Value>(&out) {
                    let rows = a
                        .into_iter()
                        .filter(|r| r.is_object() && truth(&r["status"]))
                        .map(|mut r| {
                            r["host"] = name.into();
                            r
                        })
                        .collect();
                    return (rows, vec![]);
                }
                break;
            }
            Err(e) if attempt == 1 => {
                eprintln!("fleet_hosts: probe of {name} failed ({e}), retrying once");
                pause();
            }
            Err(_) => break,
        }
    }
    (
        vec![],
        vec![format!(
            "host {name} unreachable (sessions unknown, not dead)"
        )],
    )
}

pub fn remote_sessions() -> (Vec<Value>, Vec<String>) {
    let policy = std::env::var_os("FLEET_HOST_POLICY")
        .map(PathBuf::from)
        .unwrap_or_else(|| crate::data_home().join("ops/fleet-launch-policy.json"));
    let policy = crate::read_json(&policy, 64 << 20).unwrap_or(Value::Null);
    let local = std::env::var("FLEET_LOCAL_HOST")
        .or_else(|_| std::env::var("HOSTNAME"))
        .or_else(|_| std::env::var("COMPUTERNAME"))
        .unwrap_or_else(|_| {
            crate::process::run("hostname", &[], 5)
                .map(|o| o.stdout.trim().to_owned())
                .unwrap_or_default()
        });
    let (mut rows, mut errors) = (vec![], vec![]);
    for h in policy["hosts"].as_array().into_iter().flatten() {
        let name = string(h, "name");
        let aliases = h["aliases"].as_array();
        if name == local
            || name.split('.').next() == local.split('.').next()
            || aliases.is_some_and(|a| a.iter().any(|v| v.as_str() == Some(&local)))
            || h["enabled"] == false
        {
            continue;
        }
        let (r, e) = probe_remote(
            name,
            || {
                let p = crate::process::run(
                    "ssh",
                    &[
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "ConnectTimeout=5",
                        string(h, "ssh"),
                        "bash -lc 'claude agents --json'",
                    ],
                    10,
                )?;
                if p.code != 0 {
                    Err("OSError".into())
                } else {
                    Ok(p.stdout)
                }
            },
            || std::thread::sleep(std::time::Duration::from_secs(2)),
        );
        rows.extend(r);
        errors.extend(e);
    }
    (rows, errors)
}

pub fn main(args: &[String]) -> i32 {
    if args.len() != 1 {
        eprintln!("{}", include_str!("usage/fleet-name.txt").trim());
        return 2;
    }
    let role = role_of(&args[0]);
    let local = crate::process::run("claude", &["agents", "--json"], 30)
        .ok()
        .and_then(|o| serde_json::from_str(&o.stdout).ok())
        .unwrap_or(Value::Null);
    let (remote, errors) = remote_sessions();
    for e in errors {
        eprintln!("fleet-name: {e}");
    }
    let spec = crate::read_json(
        &routing_state().join("launch").join(format!("{role}.json")),
        64 << 20,
    )
    .unwrap_or(Value::Null);
    match choose(&role, &local, &remote, &spec) {
        Some(name) => {
            println!("{name}");
            0
        }
        None => {
            eprintln!("fleet-name: no live session or launch record for {role}");
            1
        }
    }
}
