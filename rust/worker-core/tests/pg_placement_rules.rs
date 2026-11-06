mod placement_rule_binary {
    use std::io::Write;
    use std::process::{Command, Output, Stdio};

    #[test]
    fn no_sibling_binary_target() {
        assert!(!std::path::Path::new(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/src/bin/pg-placement-rules_tests.rs"
        ))
        .exists());
    }

    #[test]
    fn documentation_uses_cargo_integration_binary() {
        let docs = include_str!("../../../docs/placement-rules.md");
        assert!(docs.contains("Cargo builds the executable for the integration tests"));
        assert!(!docs.contains("Build `cargo build -p pg-worker-core --bin pg-placement-rules`"));
    }

    fn child(input: &[u8], args: &[&str]) -> Output {
        let mut child = Command::new(env!("CARGO_BIN_EXE_pg-placement-rules"))
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
