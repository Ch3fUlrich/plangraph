use pg_hook::{observe, registry, Env, Hook, HookOutput};
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::path::Path;

#[derive(Deserialize)]
struct Corpus {
    payloads: Vec<Payload>,
}
#[derive(Deserialize)]
struct Payload {
    name: String,
    hook: String,
    stdin: String,
    #[serde(default)]
    env: BTreeMap<String, String>,
    #[serde(default)]
    files: BTreeMap<String, String>,
    #[serde(flatten)]
    expected: HookOutput,
}
fn load(path: &Path) -> Corpus {
    let text = std::fs::read_to_string(path).unwrap();
    // No port/redactor runs in this slice: retain symbolic secret fixtures.
    serde_json::from_str(&text).unwrap()
}
fn differential(corpus: &Corpus, hooks: &BTreeMap<&str, &dyn Hook>) -> Vec<Value> {
    let mut records = Vec::new();
    for case in &corpus.payloads {
        let entry = match case.hook.as_str() {
            "fleet_inject" if case.stdin.contains("PostToolUse") => "fleet_inject_post",
            "fleet_inject" => "fleet_inject_pre",
            "context_guard" => "ctx_guard_session",
            other => other,
        };
        let Some(hook) = hooks.get(entry) else {
            continue;
        };
        let tmp = tempfile::tempdir().unwrap();
        let sub = |text: &str| text.replace("{TMP}", tmp.path().to_str().unwrap());
        for (relative, content) in &case.files {
            let path = tmp.path().join(relative);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(path, sub(content)).unwrap();
        }
        let mut env = Env::from([
            ("HOME".into(), tmp.path().as_os_str().to_owned()),
            (
                "XDG_STATE_HOME".into(),
                tmp.path().join(".local/state").into_os_string(),
            ),
        ]);
        env.extend(case.env.iter().map(|(k, v)| (k.into(), sub(v).into())));
        let rust = observe(*hook, sub(&case.stdin).as_bytes(), &env);
        if rust == json!({"status": "NotImplemented"}) {
            continue;
        }
        let py = serde_json::to_value(&case.expected).unwrap();
        records.push(
            json!({"entry": entry, "same": py == rust, "py": py, "rust": rust, "name": case.name}),
        );
    }
    records
}
#[test]
fn real_payload_loader_and_differential_driver() {
    let corpus =
        load(&Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/hooks/parity_payloads.json"));
    assert_eq!(corpus.payloads.len(), 29);
    assert!(corpus
        .payloads
        .iter()
        .any(|p| p.name == "fi-ups-seeded-inbox" && !p.files.is_empty()));
    assert!(
        corpus
            .payloads
            .iter()
            .flat_map(|p| p.files.values())
            .any(|text| text.contains("{TOK}")),
        "shadow-only fixtures must retain symbolic secrets, not construct token strings"
    );
    let pending = registry();
    assert!(differential(&corpus, &pending).is_empty());
    // Independently return the no-op result recorded for these two hook groups.
    // Pending production hooks do not count as exercised parity comparisons.
    struct Noop;
    impl Hook for Noop {
        fn run(&self, event_json: &[u8], env: &Env) -> Result<HookOutput, pg_hook::HookError> {
            let home = Path::new(&env[std::ffi::OsStr::new("HOME")]);
            assert!(home.is_dir());
            assert_eq!(
                Path::new(&env[std::ffi::OsStr::new("XDG_STATE_HOME")]),
                home.join(".local/state")
            );
            assert!(!String::from_utf8_lossy(event_json).contains("{TMP}"));
            if let Ok(value) = serde_json::from_slice::<Value>(event_json) {
                if let Some(path) = value["transcript_path"].as_str() {
                    assert!(Path::new(path).starts_with(home));
                    assert!(std::fs::read_to_string(path).unwrap().contains("assistant"));
                }
                if value["session_id"] == "fake-parent" {
                    assert_eq!(
                        env[std::ffi::OsStr::new("FLEET_SESSION_NAME")],
                        "fake-parent"
                    );
                    let inbox = home.join(".local/state/fleet/inbox/fake-parent.jsonl");
                    if inbox.exists() {
                        let text = std::fs::read_to_string(inbox).unwrap();
                        assert_eq!(text.lines().count(), 2);
                        assert!(text.contains("{TOK}"));
                        let cursor = home.join(".local/state/fleet/inbox/.cursor/fake-parent");
                        if value["hook_event_name"] == "PostToolUse" {
                            assert_eq!(std::fs::read_to_string(cursor).unwrap(), "110");
                        } else {
                            assert!(!cursor.exists());
                        }
                    }
                }
            }
            Ok(HookOutput {
                exit_code: 0,
                stdout: String::new(),
                stderr: String::new(),
            })
        }
    }
    let mut hooks = pending;
    hooks.insert("fleet_report", &Noop);
    hooks.insert("ctx_guard_session", &Noop);
    let records = differential(&corpus, &hooks);
    assert_eq!(records.len(), 11);
    for record in records {
        assert_eq!(record["same"], true, "{record}");
    }
    struct Different;
    impl Hook for Different {
        fn run(&self, _: &[u8], _: &Env) -> Result<HookOutput, pg_hook::HookError> {
            Ok(HookOutput {
                exit_code: 99,
                stdout: "different".into(),
                stderr: String::new(),
            })
        }
    }
    let hooks = BTreeMap::from([("bash_guard", &Different as &dyn Hook)]);
    let records = differential(&corpus, &hooks);
    assert_eq!(records.len(), 12);
    assert!(records
        .iter()
        .all(|r| r["same"] == false && r["rust"]["exit"] == 99));

    // Exercise both inject aliases with an independent no-op, including seeded
    // positive/negative outputs, without implementing a production hook.
    let hooks = BTreeMap::from([
        ("fleet_inject_pre", &Noop as &dyn Hook),
        ("fleet_inject_post", &Noop as &dyn Hook),
    ]);
    let records = differential(&corpus, &hooks);
    assert_eq!(records.len(), 6);
    assert_eq!(
        records
            .iter()
            .filter(|r| r["entry"] == "fleet_inject_pre")
            .count(),
        4
    );
    assert_eq!(
        records
            .iter()
            .filter(|r| r["entry"] == "fleet_inject_post")
            .count(),
        2
    );
    assert_eq!(records.iter().filter(|r| r["same"] == true).count(), 4);
    for record in records.iter().filter(|r| r["same"] == false) {
        assert!(matches!(
            record["name"].as_str().unwrap(),
            "fi-ups-seeded-inbox" | "fi-ptu-cursor-offset"
        ));
        let output: Value = serde_json::from_str(record["py"]["stdout"].as_str().unwrap()).unwrap();
        assert!(output["hookSpecificOutput"]["additionalContext"]
            .as_str()
            .unwrap()
            .contains("[redacted]"));
    }
}
