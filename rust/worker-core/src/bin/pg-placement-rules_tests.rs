// Cargo also discovers this sibling as a binary target.
#[cfg(not(test))]
fn main() {}

#[cfg(test)]
mod placement_rule_binary {
    use std::io::Write;
    use std::path::PathBuf;
    use std::process::{Command, Output, Stdio};
    use std::sync::OnceLock;

    fn executable() -> &'static PathBuf {
        static EXECUTABLE: OnceLock<PathBuf> = OnceLock::new();
        EXECUTABLE.get_or_init(|| {
            let test = std::env::current_exe().unwrap();
            let target = test.parent().unwrap().parent().unwrap().parent().unwrap();
            // Unit-test targets do not cause Cargo to build the runnable binary.
            let build = Command::new(env!("CARGO"))
                .args([
                    "build",
                    "--locked",
                    "--package",
                    "pg-worker-core",
                    "--bin",
                    "pg-placement-rules",
                ])
                .arg("--manifest-path")
                .arg(concat!(env!("CARGO_MANIFEST_DIR"), "/Cargo.toml"))
                .arg("--target-dir")
                .arg(target)
                .output()
                .unwrap();
            assert!(
                build.status.success(),
                "{}",
                String::from_utf8_lossy(&build.stderr)
            );
            target.join("debug").join(format!(
                "pg-placement-rules{}",
                std::env::consts::EXE_SUFFIX
            ))
        })
    }

    fn child(input: &[u8], args: &[&str]) -> Output {
        let mut child = Command::new(executable())
            .args(args)
            .env_clear()
            .env("PG_RULE_HOST_WORKER_CAP_COUNT", "1")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        // Refusal may close stdin before the writer completes.
        let _ = child.stdin.take().unwrap().write_all(input);
        child.wait_with_output().unwrap()
    }

    fn refused(output: Output) {
        assert_eq!(output.status.code(), Some(2));
        assert_eq!(output.stdout, b"{\"error\":\"rules_invalid\"}\n");
        assert!(output.stderr.is_empty());
    }

    #[test]
    fn bounded_stdin_one_response() {
        let mut input = br#"{"op":"specs"}"#.to_vec();
        input.resize(1024 * 1024, b' ');
        let output = child(&input, &[]);
        assert!(output.status.success());
        assert_eq!(output.stdout.iter().filter(|b| **b == b'\n').count(), 1);
        let value: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
        assert!(value["ok"]["specs"].is_array());
        input.push(b' ');
        refused(child(&input, &[]));
        refused(child(b"{} {}", &[]));
    }

    #[test]
    fn arguments_refused() {
        refused(child(b"", &["--help"]));
    }

    #[test]
    fn malformed_input_no_echo() {
        let secret = ["fake-", "private-", "value"].concat();
        refused(child(format!("{{{secret}").as_bytes(), &[]));
        refused(child(
            serde_json::json!({"op":"specs","actor":secret})
                .to_string()
                .as_bytes(),
            &[],
        ));
    }

    #[test]
    fn real_child_exits_and_is_reaped() {
        for _ in 0..3 {
            let output = child(br#"{"op":"specs"}"#, &[]);
            assert_eq!(output.status.code(), Some(0));
            assert!(output.stderr.is_empty());
        }
    }
}
