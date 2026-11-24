//! A state-shadow role keeps its measure hook and its ROLE.json across launches.
use super::sandbox::{Launcher, Sandbox, SID};
use serde_json::{json, Value};
use std::fs;

const EVENTS: [&str; 5] = [
    "UserPromptSubmit",
    "Notification",
    "PreToolUse",
    "PostToolUse",
    "Stop",
];

fn launch(s: &Sandbox, role: &str) -> Value {
    let out = s
        .launch(role)
        .args(["--ctx-guard", "110000,170000"])
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(0), "{}", s.normalize(&out.stderr));
    s.json("child.json")
}

fn settings_path(child: &Value) -> String {
    let argv = child["argv"].as_array().unwrap();
    let at = argv.iter().position(|arg| arg == "--settings").unwrap();
    argv[at + 1].as_str().unwrap().to_owned()
}

fn hooked(path: &str) -> bool {
    let settings: Value = serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap();
    EVENTS.iter().all(|event| {
        settings["hooks"][event].as_array().is_some_and(|groups| {
            groups
                .iter()
                .any(|g| g.to_string().contains("--measure-only"))
        })
    })
}

pub fn check(launcher: &Launcher) {
    let s = Sandbox::new(launcher);
    let dir = s.path("home/.config/plangraph/state-shadow");
    fs::create_dir_all(&dir).unwrap();
    let stale = || {
        for role in ["routing", "plan-order"] {
            fs::write(dir.join(format!("{role}.env")), "").unwrap();
            fs::write(
                dir.join(format!("{role}.json")),
                json!({"role": role, "session_id": "stale-9", "transcript": "/gone/stale-9.jsonl", "principal": "p"})
                    .to_string(),
            )
            .unwrap();
        }
    };
    stale();
    let routing = settings_path(&launch(&s, "routing"));
    let plan_order = settings_path(&launch(&s, "plan-order"));
    let review = settings_path(&launch(&s, "review"));
    assert_ne!(routing, plan_order, "equal caps must not share a file");
    assert!(hooked(&routing) && hooked(&plan_order));
    assert!(
        !hooked(&review),
        "the hook leaked to a role without a collector"
    );
    for role in ["routing", "plan-order"] {
        let config = s.json(&format!("home/.config/plangraph/state-shadow/{role}.json"));
        assert_eq!(config["session_id"], SID);
        assert_eq!(config["principal"], "p");
        assert!(config["transcript"]
            .as_str()
            .unwrap()
            .ends_with(&format!("/{SID}.jsonl")));
    }
    // A recycle regenerates the settings and moves ROLE.json to the new session.
    stale();
    assert_eq!(settings_path(&launch(&s, "routing")), routing);
    assert!(hooked(&routing));
    let config = s.json("home/.config/plangraph/state-shadow/routing.json");
    assert_eq!(config["session_id"], SID);
}
