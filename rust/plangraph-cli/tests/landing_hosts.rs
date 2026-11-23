#![cfg(unix)]

use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Stdio},
};

fn git(root: &Path, args: &[&str]) -> String {
    let output = Command::new("git")
        .arg("-C")
        .arg(root)
        .args(args)
        .env("GIT_AUTHOR_NAME", "Fixture")
        .env("GIT_AUTHOR_EMAIL", "fixture")
        .env("GIT_COMMITTER_NAME", "Fixture")
        .env("GIT_COMMITTER_EMAIL", "fixture")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap().trim().into()
}

struct Checkout {
    directory: tempfile::TempDir,
    root: PathBuf,
    stamp: PathBuf,
    binary: PathBuf,
    hook: PathBuf,
    head: String,
}

impl Checkout {
    fn new() -> Self {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path().join("main checkout");
        let source_hooks = root.join("tools/hooks");
        fs::create_dir_all(&source_hooks).unwrap();
        git(&root, &["init", "-q", "-b", "main"]);
        fs::write(source_hooks.join("pre-push"), "merged hook\n").unwrap();
        git(&root, &["add", "tools/hooks/pre-push"]);
        git(&root, &["commit", "-qm", "fixture"]);
        let head = git(&root, &["rev-parse", "HEAD"]);
        let hook = root.join(".git/hooks/pre-push");
        fs::create_dir_all(hook.parent().unwrap()).unwrap();
        fs::copy(source_hooks.join("pre-push"), &hook).unwrap();
        let binary = directory.path().join("installed");
        fs::write(&binary, b"fixture executable bytes").unwrap();
        let stamp = directory.path().join("build.json");
        fs::write(
            &stamp,
            json!({"head":head, "binaries":[{
                "path":binary,
                "sha256": format!("{:x}", Sha256::digest(b"fixture executable bytes")),
            }]})
            .to_string(),
        )
        .unwrap();
        Self {
            directory,
            root,
            stamp,
            binary,
            hook,
            head,
        }
    }

    fn host_agent(&self, head: &str, sync: Option<&Path>) -> Value {
        let mut child = Command::new(env!("CARGO_BIN_EXE_plangraph"))
            .args(["landing", "host-agent"])
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let request = json!({"root": self.root, "stamp":self.stamp, "head":head, "sync":sync});
        child
            .stdin
            .take()
            .unwrap()
            .write_all(request.to_string().as_bytes())
            .unwrap();
        let output = child.wait_with_output().unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        serde_json::from_slice(&output.stdout).unwrap()
    }
}

#[test]
fn agent_checks_the_real_checkout_hook_stamp_and_binary_digest() {
    let checkout = Checkout::new();
    let clean = checkout.host_agent(&checkout.head, None);
    for field in ["head", "checkout", "hooks", "build"] {
        assert_eq!(clean[field], checkout.head);
    }
    assert_eq!(clean["verified"], true);
    assert_eq!(
        checkout.host_agent(&"f".repeat(40), None),
        json!({"verified":false, "reason":"main checkout differs from the merged head"}),
    );
    fs::write(&checkout.hook, "stale hook\n").unwrap();
    assert_eq!(
        checkout.host_agent(&checkout.head, None),
        json!({"verified":false, "reason":"hook pre-push differs from the merged tree"}),
    );
    fs::copy(checkout.root.join("tools/hooks/pre-push"), &checkout.hook).unwrap();
    let stamp = fs::read(&checkout.stamp).unwrap();
    let mut wrong_head: Value = serde_json::from_slice(&stamp).unwrap();
    wrong_head["head"] = json!("e".repeat(40));
    fs::write(&checkout.stamp, wrong_head.to_string()).unwrap();
    assert_eq!(
        checkout.host_agent(&checkout.head, None),
        json!({"verified":false, "reason":"build stamp differs from the merged head or lists no binaries"}),
    );
    fs::write(&checkout.stamp, stamp).unwrap();
    fs::write(&checkout.binary, "changed installed binary").unwrap();
    assert_eq!(
        checkout.host_agent(&checkout.head, None),
        json!({"verified":false, "reason":"a stamped installed binary differs"}),
    );
}

#[test]
fn agent_runs_sync_with_the_checkout_and_head_then_rechecks_the_hook() {
    let checkout = Checkout::new();
    fs::write(&checkout.hook, "stale hook\n").unwrap();
    let sync = checkout.directory.path().join("sync");
    pg_worker_core::executable_test_support::write_private_executable(
        &sync,
        "#!/bin/sh\nset -eu\nprintf '%s\\n' \"$1\" \"$2\" > \"$1/sync-receipt\"\ncp \"$1/tools/hooks/pre-push\" \"$1/.git/hooks/pre-push\"\n",
    );
    assert_eq!(
        checkout.host_agent(&checkout.head, Some(&sync))["verified"],
        true
    );
    let receipt = fs::read_to_string(checkout.root.join("sync-receipt")).unwrap();
    assert_eq!(
        receipt,
        format!("{}\n{}\n", checkout.root.display(), checkout.head)
    );
    assert_eq!(fs::read_to_string(&checkout.hook).unwrap(), "merged hook\n");
}
