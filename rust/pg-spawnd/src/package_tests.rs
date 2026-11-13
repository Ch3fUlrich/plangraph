use super::*;
use serde_json::json;

#[derive(Default)]
struct Fake {
    calls: Vec<(String, String)>,
    windows: bool,
}
impl Backend for Fake {
    fn keeper(&mut self, verb: &str, key: &str) -> Result<Value, ToolError> {
        self.calls.push((verb.into(), key.into()));
        if key.ends_with("STALE") {
            return Ok(json!({"status":"refused", "reason":"already merged"}));
        }
        Ok(
            json!({"key":key, "title":"Implement sample", "description":"Sample body", "done_criteria":"Check it"}),
        )
    }
    fn remote(&mut self, _: &Host, script: &str) -> Result<Value, ToolError> {
        self.calls.push(("remote".into(), script.into()));
        Ok(if self.windows {
            json!({"pid":42, "task":"package", "alive":true})
        } else {
            json!({"pane":"pane-1", "alive":true})
        })
    }
}
fn temp() -> std::path::PathBuf {
    let p = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    std::fs::create_dir_all(&p).unwrap();
    p
}
fn host(windows: bool) -> Host {
    Host {
        kind: if windows { "windows" } else { "linux" }.into(),
        target: "fixture".into(),
        repo: "/tmp/repo".into(),
        root: "/tmp/packages".into(),
    }
}
#[test]
fn package_launch_claims_first_and_uses_medium() {
    for windows in [false, true] {
        let state = temp();
        let mut backend = Fake {
            windows,
            ..Fake::default()
        };
        let args = json!({"host":"fixture", "base_ref":"origin/main", "branch":"phase/fixture", "keys":["p:ACTIVE","p:STALE"]});
        let result = launch(
            &state,
            args.as_object().unwrap(),
            &host(windows),
            &mut backend,
        )
        .unwrap_or_else(|_| panic!("launch"));
        assert_eq!(result["keys"], json!(["p:ACTIVE"]));
        assert_eq!(result["refused"][0]["reason"], "already merged");
        assert_eq!(backend.calls[0], ("claim".into(), "p:ACTIVE".into()));
        let script = backend
            .calls
            .iter()
            .filter(|(verb, _)| verb == "remote")
            .map(|(_, s)| s.as_str())
            .collect::<Vec<_>>()
            .join("\n");
        assert!(script.contains("model_reasoning_effort=medium"));
        assert!(!result["package_text"].as_str().unwrap().contains("STALE"));
        assert!(script.contains(if windows { "schtasks" } else { "herdr" }));
        assert!(script.contains("fetch"));
        assert!(script.contains("user.email"));
        assert_eq!(
            read(&state, result["run_id"].as_str().unwrap()).unwrap_or_else(|_| panic!("read"))
                ["branch"],
            "phase/fixture"
        );
        std::fs::remove_dir_all(state).unwrap();
    }
}
#[test]
fn package_refuses_bad_effort_and_unsafe_refs_before_claiming() {
    let state = temp();
    let mut fake = Fake::default();
    for patch in [
        json!({"effort":"high"}),
        json!({"branch":"phase/x; echo injected"}),
    ] {
        let mut args = json!({"host":"fixture", "base_ref":"origin/main", "branch":"phase/test", "keys":["p:OK"]});
        args.as_object_mut()
            .unwrap()
            .extend(patch.as_object().unwrap().clone());
        assert!(launch(&state, args.as_object().unwrap(), &host(false), &mut fake).is_err());
        assert!(fake.calls.is_empty());
    }
    std::fs::remove_dir_all(state).unwrap();
}

#[cfg(unix)]
#[test]
fn package_linux_script_runs_against_command_fakes() {
    use std::os::unix::fs::PermissionsExt;
    let state = temp();
    let bin = state.join("bin");
    std::fs::create_dir(&bin).unwrap();
    for (name,script) in [
        ("git", "#!/bin/sh\ncase \"$*\" in\n'symbolic-ref --short HEAD') echo main;;\n'config user.name') echo Test;;\n'config user.email') echo '123+writer@users.noreply.github.com';;\n'var GIT_'*) echo 'Fixture <123+writer@users.noreply.github.com> 1 +0000';;\n'worktree add -b '*) mkdir -p \"$5\";;\nesac\n"),
        ("herdr", "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$PKG_CALLS\"\ncase \"$*\" in 'workspace create '*) echo pane-1;; esac\n"),
        ("sleep", "#!/bin/sh\nexit 0\n")
    ] {
        let path=bin.join(name);std::fs::write(&path,script).unwrap();
        std::fs::set_permissions(path,std::fs::Permissions::from_mode(0o755)).unwrap();
    }
    let h = Host {
        repo: state.to_string_lossy().into(),
        root: state.join("packages' space").to_string_lossy().into(),
        ..host(false)
    };
    let args =
        json!({"host":"fixture","base_ref":"origin/main","branch":"phase/test","keys":["p:OK"]});
    let mut fake = Fake::default();
    let result = launch(&state, args.as_object().unwrap(), &h, &mut fake)
        .unwrap_or_else(|_| panic!("launch"));
    let output = std::process::Command::new("sh")
        .arg("-c")
        .arg(format!(
            "{}\n{}",
            crate::package_scripts::launch(&h, &result),
            crate::package_scripts::start_linux(&result)
        ))
        .env("PATH", format!("{}:/usr/bin:/bin", bin.display()))
        .env("PKG_CALLS", state.join("calls"))
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        std::fs::read_to_string(result["package_path"].as_str().unwrap())
            .unwrap()
            .contains("p:OK")
    );
    assert!(String::from_utf8_lossy(&output.stdout).contains("pane-1"));
    std::fs::remove_dir_all(state).unwrap();
}

#[test]
fn package_all_stale_claims_return_reasons_without_remote_work() {
    let state = temp();
    let mut fake = Fake::default();
    let args = json!({"host":"fixture","base_ref":"origin/main","branch":"phase/test","keys":["p:STALE","p:STALE"]});
    let result = launch(&state, args.as_object().unwrap(), &host(false), &mut fake)
        .unwrap_or_else(|_| panic!("refusal list"));
    assert_eq!(result["status"], "refused");
    assert_eq!(
        result["refused"],
        json!([{"key":"p:STALE","reason":"already merged"}])
    );
    assert!(result.get("package_text").is_none());
    assert_eq!(fake.calls, vec![("claim".into(), "p:STALE".into())]);
    assert!(list(&state).is_empty());
    std::fs::remove_dir_all(state).unwrap();
}
