use super::*;
#[test]
fn final_head_review_ignores_old_head_and_requires_last_canonical_verdict() {
    let dir = std::env::temp_dir().join(format!("native-review-{}", std::process::id()));
    fs::create_dir_all(&dir).unwrap();
    fs::write(dir.join("aaaaaaaa-hydrogen.txt"), "VERDICT: ACCEPT\n").unwrap();
    assert!(find_review(&dir, "bbbbbbbbbbbbbbbb", &[])
        .unwrap()
        .is_none());
    fs::write(
        dir.join("bbbbbbbb-hydrogen.txt"),
        "VERDICT: ACCEPT\nVERDICT: UNKNOWN\n",
    )
    .unwrap();
    assert!(find_review(&dir, "bbbbbbbbbbbbbbbb", &[])
        .unwrap()
        .is_none());
    fs::write(
        dir.join("bbbbbbbb-hydrogen.txt"),
        "VERDICT: REQUEST_CHANGES\n",
    )
    .unwrap();
    assert_eq!(
        find_review(&dir, "bbbbbbbbbbbbbbbb", &[]).unwrap().unwrap()["verdict"],
        "REQUEST_CHANGES"
    );
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn stored_accept_without_reviewed_file_is_no_verdict() {
    let dir = tempfile::tempdir().unwrap();
    let prefix = "I am running tests in the background.\n";
    let tail = "\nFINDING none\nVERDICT: ACCEPT\n";
    let text = format!(
        "{prefix}{}{tail}",
        " ".repeat(288 - prefix.len() - tail.len())
    );
    assert_eq!(text.len(), 288);
    fs::write(dir.path().join("aaaaaaaa-argon.txt"), text).unwrap();
    let review = find_review(dir.path(), "aaaaaaaaaaaaaaaa", &[])
        .unwrap()
        .unwrap();
    assert_eq!(review["status"], "no_verdict");
    assert_eq!(review["reason"], "weak_verdict");
    assert_ne!(review["verdict"], "ACCEPT");
}

#[test]
fn stored_accept_with_changed_path_or_file_line_is_accepted() {
    let dir = tempfile::tempdir().unwrap();
    for citation in ["Reviewed src/lib.rs", "Reviewed Makefile:12"] {
        fs::write(
            dir.path().join("aaaaaaaa-argon.txt"),
            format!("{citation}\nFINDING none\nVERDICT: ACCEPT\n"),
        )
        .unwrap();
        assert_eq!(
            find_review(dir.path(), "aaaaaaaaaaaaaaaa", &["src/lib.rs".into()])
                .unwrap()
                .unwrap()["verdict"],
            "ACCEPT"
        );
    }
}

#[test]
fn native_weak_review_exits_as_missing_verdict_without_running_gates() {
    struct Weak;
    impl crate::native_writer::Driver for Weak {
        fn prepare(&mut self) -> Result<String, String> {
            Ok("base".into())
        }
        fn refresh_base(&mut self) -> Result<String, String> {
            panic!("weak review must not relaunch the writer")
        }
        fn disk_gb(&mut self) -> Result<u64, String> {
            Ok(5)
        }
        fn execute(&mut self, session: Option<&str>, _: &str) -> Result<String, String> {
            assert!(session.is_none(), "weak review must not resume the writer");
            Ok("session".into())
        }
        fn head(&mut self) -> Result<String, String> {
            Ok("head".into())
        }
        fn review(&mut self, _: &str) -> Result<Option<Value>, String> {
            Ok(Some(
                json!({"status":"no_verdict", "verdict":"no_verdict", "reason":"weak_verdict"}),
            ))
        }
        fn gates(&mut self) -> Result<(), String> {
            panic!("weak review cannot authorize final gates")
        }
        fn save(&mut self, _: &Value) -> Result<(), String> {
            Ok(())
        }
    }
    let record = crate::native_writer::run(&mut Weak, json!({}), "brief");
    assert_eq!(record["exit_code"], 4);
    assert_eq!(record["reason"], "weak_verdict");
    assert_eq!(record["review"]["status"], "no_verdict");
}
