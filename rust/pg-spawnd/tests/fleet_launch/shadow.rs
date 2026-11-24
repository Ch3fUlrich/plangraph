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

const NEXT_SID: &str = "87654321-4321-4321-4321-cba987654321";

fn project_dir(s: &Sandbox, cwd: &str) -> String {
    let path = s.path(cwd);
    let name: String = path
        .to_string_lossy()
        .chars()
        .map(|ch| if ch.is_ascii_alphanumeric() { ch } else { '-' })
        .collect();
    s.path(&format!("home/.claude/projects/{name}"))
        .display()
        .to_string()
}

pub fn check(launcher: &Launcher) {
    let s = Sandbox::new(launcher);
    let dir = s.path("home/.config/plangraph/state-shadow");
    fs::create_dir_all(&dir).unwrap();
    // The routing path has a space inside and one at its end; the plan-order path is continued
    // across two lines. The service reads both as the configured path.
    let custom = s.path("custom dir/routing collector.json ");
    let continued = s.path("po-cfg/plan-order-collector.json");
    let location = |role: &str| match role {
        "routing" => custom.clone(),
        _ => continued.clone(),
    };
    let stale = || {
        for role in ["routing", "plan-order"] {
            // The routing collector's env file moves its configuration away from the default.
            let env = if role == "routing" {
                format!(
                    "PLANGRAPH_STATE_CONFIG = {}\n",
                    custom.display().to_string().replace(' ', "\\ ")
                )
            } else {
                let path = continued.display().to_string();
                let (head, tail) = path.split_at(path.len() - "collector.json".len());
                format!("PLANGRAPH_STATE_CONFIG={head}\\\n{tail}\n")
            };
            fs::write(dir.join(format!("{role}.env")), env).unwrap();
            fs::create_dir_all(location(role).parent().unwrap()).unwrap();
            fs::write(
                location(role),
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
        let config: Value =
            serde_json::from_str(&fs::read_to_string(location(role)).unwrap()).unwrap();
        assert_eq!(config["session_id"], SID, "{role}");
        assert_eq!(config["principal"], "p");
        assert!(config["transcript"]
            .as_str()
            .unwrap()
            .ends_with(&format!("/{SID}.jsonl")));
    }
    for role in ["routing", "plan-order"] {
        assert!(
            !dir.join(format!("{role}.json")).exists(),
            "the default file of {role} was written beside the configured one"
        );
    }
    // A recycle in another worktree with another session id regenerates the settings and moves
    // ROLE.json to the new transcript.
    fs::create_dir(s.path("work2")).unwrap();
    fs::write(s.path("child.stdout"), format!("{NEXT_SID}\n")).unwrap();
    fs::remove_file(&routing).unwrap();
    let out = s
        .launch_at("routing", "work2")
        .args(["--ctx-guard", "110000,170000"])
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(0), "{}", s.normalize(&out.stderr));
    assert_eq!(settings_path(&s.json("child.json")), routing);
    assert!(hooked(&routing), "the hook is regenerated after a relaunch");
    let config: Value = serde_json::from_str(&fs::read_to_string(&custom).unwrap()).unwrap();
    assert_eq!(config["session_id"], NEXT_SID);
    assert_eq!(
        config["transcript"],
        format!("{}/{NEXT_SID}.jsonl", project_dir(&s, "work2"))
    );
}
