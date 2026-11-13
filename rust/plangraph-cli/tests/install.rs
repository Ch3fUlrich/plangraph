use plangraph_cli::install::{self, Host};
use std::collections::BTreeSet;
#[derive(Default)]
struct FakeHost {
    installed: BTreeSet<String>,
    calls: Vec<String>,
    fail: bool,
}
impl Host for FakeHost {
    fn detected(&mut self, binary: &str) -> bool {
        self.installed.contains(binary)
    }
    fn apt(&mut self, packages: &[&str]) -> Result<(), String> {
        self.calls.push(format!("apt {}", packages.join(" ")));
        if self.fail {
            return Err("fixture failure".into());
        }
        self.installed.insert(
            if packages[0] == "python3" {
                "python3"
            } else {
                "git"
            }
            .into(),
        );
        Ok(())
    }
    fn npm(&mut self, package: &str) -> Result<(), String> {
        self.calls.push(format!("npm {package}"));
        self.installed.insert(
            match package {
                "@opencode/cli" => "opencode",
                "@anthropic-ai/claude-code" => "claude",
                "@qwen-code/qwen-code" => "qwen",
                "@google/gemini-cli" => "gemini",
                _ => panic!("unknown package"),
            }
            .into(),
        );
        Ok(())
    }
    fn script(&mut self, url: &str, privileged: bool) -> Result<(), String> {
        self.calls
            .push(format!("script {url} privileged={privileged}"));
        self.installed.insert(
            if url.contains("nodesource") {
                "node"
            } else if url.contains("tailscale") {
                "tailscale"
            } else {
                "qodercli"
            }
            .into(),
        );
        Ok(())
    }
}
#[test]
fn disposable_fresh_host_real_execution_and_second_run_skips_all() {
    let mut host = FakeHost::default();
    let (code, rows) = install::run(&mut host, None, false, false);
    assert_eq!(code, 0);
    assert_eq!(rows.len(), 9);
    assert!(rows.iter().all(|s| s.contains("installed verified")));
    assert_eq!(host.calls.len(), 9);
    let calls = host.calls.clone();
    let (code, rows) = install::run(&mut host, None, false, false);
    assert_eq!(code, 0);
    assert!(rows.iter().all(|s| s.contains("skipped")));
    assert_eq!(calls, host.calls);
}
#[test]
fn dry_run_check_selection_and_dependencies() {
    let mut host = FakeHost::default();
    assert_eq!(install::run(&mut host, None, true, false).0, 0);
    assert!(host.calls.is_empty());
    assert!(host.installed.is_empty());
    assert_eq!(install::run(&mut host, None, false, true).0, 1);
    assert!(host.calls.is_empty());
    assert_eq!(
        install::select(Some("claude-code")).unwrap(),
        ["nodejs", "claude-code"]
    );
    assert!(install::select(Some("omniroute")).is_err());
    assert!(install::select(Some("git,unknown")).is_err());
    assert_eq!(
        install::run(&mut host, Some("git,unknown"), false, false).0,
        2
    );
    assert!(host.calls.is_empty());
}
#[test]
fn installation_failure_stops_and_scripts_are_validated() {
    let mut host = FakeHost {
        fail: true,
        ..Default::default()
    };
    assert_eq!(install::run(&mut host, None, false, false).0, 3);
    assert_eq!(host.calls.len(), 1);
    assert!(install::validate_script(b"#!/bin/sh\nexit 0\n"));
    for bad in [b"".as_slice(), b"<html>error</html>", b"#!/bin/sh\n\0"] {
        assert!(!install::validate_script(bad));
    }
}
#[cfg(target_os = "linux")]
#[test]
fn real_cli_node_repository_setup_also_installs_node_package() {
    use std::os::unix::fs::{symlink, PermissionsExt};
    use std::{fs, process::Command};
    let root = tempfile::tempdir().unwrap();
    let bins = root.path().join("bin");
    fs::create_dir(&bins).unwrap();
    let write = |name: &str, text: &str| {
        let path = bins.join(name);
        fs::write(&path, text).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
    };
    write("sudo", "#!/bin/sh\nshift; shift; exec \"$@\"\n");
    write(
        "apt-get",
        r##"#!/bin/sh
printf '%s\n' "$*" >> "$HOME/apt-calls"
case " $* " in
  *" nodejs "*)
    /bin/mkdir -p "$HOME/.local/bin"
    printf '#!/bin/sh\necho v24.0.0\n' > "$HOME/.local/bin/node"
    printf '#!/bin/sh\necho 10.0.0\n' > "$HOME/.local/bin/npm"
    /bin/chmod 700 "$HOME/.local/bin/node" "$HOME/.local/bin/npm" ;;
esac
"##,
    );
    write(
        "curl",
        r##"#!/bin/sh
while [ "$#" -gt 0 ]; do
  if [ "$1" = --output ]; then shift; target="$1"; fi
  shift
done
printf '#!/bin/sh\nexit 0\n' > "$target"
"##,
    );
    symlink("/bin/bash", bins.join("bash")).unwrap();
    let invoke = |allow_unpinned| {
        Command::new(env!("CARGO_BIN_EXE_plangraph"))
            .args(["install", "--only", "nodejs", "--yes"])
            .args(if allow_unpinned {
                vec!["--allow-unpinned-scripts"]
            } else {
                vec![]
            })
            .env("HOME", root.path())
            .env("PATH", &bins)
            .env("XDG_STATE_HOME", root.path().join("state"))
            .env("PLANGRAPH_ENV_FILE", root.path().join("env"))
            .current_dir(root.path())
            .output()
            .unwrap()
    };
    let refused = invoke(false);
    assert!(!refused.status.success());
    assert!(!root.path().join("apt-calls").exists());
    let output = invoke(true);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stdout)
    );
    let calls = fs::read(root.path().join("apt-calls")).unwrap();
    assert!(String::from_utf8_lossy(&calls).contains("install -y -- nodejs"));
    let output = invoke(true);
    assert!(output.status.success());
    assert!(String::from_utf8_lossy(&output.stdout).contains("skipped"));
    assert_eq!(fs::read(root.path().join("apt-calls")).unwrap(), calls);
}
#[cfg(target_os = "linux")]
#[test]
fn python_without_owned_modules_is_not_skipped() {
    use std::os::unix::fs::PermissionsExt;
    use std::{fs, process::Command};
    let root = tempfile::tempdir().unwrap();
    let bins = root.path().join("bin");
    fs::create_dir(&bins).unwrap();
    for (name, text) in [
        (
            "python3",
            "#!/bin/sh\nif [ \"$1\" = --version ]; then echo Python3; exit 0; fi\nexit 1\n",
        ),
        ("sudo", "#!/bin/sh\nshift; shift; exec \"$@\"\n"),
        (
            "apt-get",
            r##"#!/bin/sh
case " $* " in
  *" python3 "*) printf '#!/bin/sh\nexit 0\n' > "$HOME/bin/python3"; /bin/chmod 700 "$HOME/bin/python3" ;;
esac
"##,
        ),
    ] {
        let path = bins.join(name);
        fs::write(&path, text).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
    }
    let invoke = |flags: &[&str]| {
        Command::new(env!("CARGO_BIN_EXE_plangraph"))
            .args(["install", "--only", "python"])
            .args(flags)
            .env("HOME", root.path())
            .env("PATH", &bins)
            .env("XDG_STATE_HOME", root.path().join("state"))
            .env("PLANGRAPH_ENV_FILE", root.path().join("env"))
            .output()
            .unwrap()
    };
    let preview = invoke(&["--check"]);
    assert_eq!(preview.status.code(), Some(1));
    assert!(!root.path().join("state").exists());
    assert!(invoke(&["--yes"]).status.success());
    assert!(String::from_utf8_lossy(&invoke(&["--check"]).stdout).contains("skipped"));
}
