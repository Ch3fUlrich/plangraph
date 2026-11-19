//! Generated host files: run-one.sh and the apt-only sudoers rule.

const RUN_ONE: &str = r#"#!/usr/bin/env bash
set -u
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
export PATH=$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin
export TEMPORAL_TEST_ADDRESS=@TEMPORAL_TEST_ADDRESS@
export RUNNER_TOOL_CACHE=$HOME/hostedtoolcache
export AGENT_TOOLSDIRECTORY=$HOME/hostedtoolcache
if [ ! -x "$HOME/.cargo/bin/rustup" ]; then
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable
fi
JIT=$(cat "$HOME/jit")
rm -f "$HOME/jit"
rm -rf "$HOME/runner/_work"
cd "$HOME/runner" && ./run.sh --jitconfig "$JIT"
rc=$?
rm -rf "$HOME/runner/_work"
exit $rc
"#;

/// The workflow owns CARGO_TARGET_DIR, so the script never sets it.
pub fn run_one(temporal_test_address: &str) -> String {
    RUN_ONE.replace("@TEMPORAL_TEST_ADDRESS@", temporal_test_address)
}

/// The one command a runner may run as root: the workflow's protobuf fallback
/// install. sudoers matches the argument list exactly, so no other apt-get
/// invocation (and no extra argument) is allowed.
pub const SUDO_COMMAND: &str =
    "/usr/bin/apt-get -o DPkg::Lock::Timeout=600 install -y protobuf-compiler libprotobuf-dev";

pub fn sudoers(users: &[String]) -> String {
    let mut out =
        String::from("# managed by plangraph runner install: one fixed apt-get command\n");
    for user in users {
        out.push_str(&format!("{user} ALL=(root) NOPASSWD: {SUDO_COMMAND}\n"));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn run_one_has_the_cache_env_and_leaves_cargo_target_to_the_workflow() {
        let s = run_one("127.0.0.1:1");
        assert!(s.contains("export TEMPORAL_TEST_ADDRESS=127.0.0.1:1\n"));
        assert!(s.contains("export RUNNER_TOOL_CACHE=$HOME/hostedtoolcache\n"));
        assert!(s.contains("export AGENT_TOOLSDIRECTORY=$HOME/hostedtoolcache\n"));
        assert!(s.contains("./run.sh --jitconfig \"$JIT\""));
        assert!(s.contains("rm -f \"$HOME/jit\""));
        assert!(!s.contains("CARGO_TARGET_DIR"));
        assert!(!s.contains('@'));
    }
    #[test]
    fn sudoers_grants_one_fixed_command_per_user() {
        let s = sudoers(&["runner1".into(), "runner2".into()]);
        for u in ["runner1", "runner2"] {
            assert!(s.contains(&format!("{u} ALL=(root) NOPASSWD: {SUDO_COMMAND}\n")));
        }
        assert_eq!(s.matches("NOPASSWD").count(), 2);
        for line in s.lines().filter(|l| l.contains("NOPASSWD")) {
            let cmd = line.split("NOPASSWD: ").nth(1).unwrap();
            assert_eq!(cmd, SUDO_COMMAND);
            // one command, no list, no wildcard, no shell metacharacter, no ALL
            assert!(!cmd.contains([',', '*', '?', '[', '\\', ';', '|', '&', '$', '`']));
            assert!(!cmd.split(' ').any(|w| w == "ALL" || w == "!"));
            assert!(cmd.starts_with("/usr/bin/apt-get "));
            // every argument is a fixed literal; the package names are the config defaults
            assert!(cmd.ends_with(" install -y protobuf-compiler libprotobuf-dev"));
        }
        assert!(!s.contains("ALL=(ALL)") && !s.contains("NOPASSWD: ALL"));
        assert!(!s.contains("/usr/bin/apt,") && !s.contains("/usr/bin/apt\n"));
    }
}
