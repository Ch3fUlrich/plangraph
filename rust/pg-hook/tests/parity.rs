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
    serde_json::from_str(&text.replace("{TOK}", &format!("sk-{}", "abcdefghijklmnopqrstuvwx")))
        .unwrap()
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
        let rust = observe(*hook, &sub(&case.stdin), &env);
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
    for record in differential(&corpus, &registry()) {
        assert_eq!(record["same"], true, "{record}");
    }
    struct Different;
    impl Hook for Different {
        fn run(&self, _: &str, _: &Env) -> Result<HookOutput, pg_hook::HookError> {
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
}
