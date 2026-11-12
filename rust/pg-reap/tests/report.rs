use serde_json::json;
use std::process::{Command, Stdio};
use std::{fs, io::Write};

fn run_report(value: &serde_json::Value) -> std::process::Output {
    let mut child = Command::new(env!("CARGO_BIN_EXE_pg-reap"))
        .arg("report")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    write!(child.stdin.take().unwrap(), "{value}").unwrap();
    child.wait_with_output().unwrap()
}

fn assert_rejected(value: &serde_json::Value, reason: &str) {
    let output = run_report(value);
    assert_eq!(output.status.code(), Some(2), "{:?}", output);
    assert!(output.stdout.is_empty(), "{:?}", output.stdout);
    assert!(String::from_utf8(output.stderr).unwrap().contains(reason));
}

#[test]
fn duplicate_paths_across_repositories_fail_without_partial_output() {
    for path in ["/shared", "\"/shar\\145d\""] {
        let repos = json!([
            {"repo":"a","porcelain":"worktree /shared\nbare\n","facts":{}},
            {"repo":"b","porcelain":format!("worktree {path}\nbare\n"),"facts":{}}
        ]);
        assert_rejected(&repos, "duplicate worktree path");
    }
}

#[test]
fn duplicate_repository_and_local_path_fail_without_partial_output() {
    let duplicate_repo = json!([
        {"repo":"a","porcelain":"worktree /a\nbare\n","facts":{}},
        {"repo":"a","porcelain":"worktree /b\nbare\n","facts":{}}
    ]);
    assert_rejected(&duplicate_repo, "duplicate repository");
    let duplicate_path = json!([
        {"repo":"a","porcelain":"worktree /a\nbare\n","facts":{}},
        {"repo":"b","porcelain":"worktree /b\nbare\n\nworktree \"/\\142\"\nbare\n","facts":{}}
    ]);
    assert_rejected(&duplicate_path, "duplicate path");
}

#[test]
fn stray_and_foreign_facts_fail_without_partial_output() {
    let f = json!({"idle_hours":25,"clean":true,"merged_into_main":true,"pushed":true,"failed":false,"has_claim":false,"live_run":false,"open_pr":false,"in_use_by_process":false,"in_use_by_unit":false,"is_clone_path":false});
    for path in ["/absent", "/a"] {
        let mut repos = json!([
            {"repo":"a","porcelain":"worktree /a\nbare\n","facts":{}},
            {"repo":"b","porcelain":"worktree /b\nbare\n","facts":{}}
        ]);
        repos[1]["facts"][path] = f.clone();
        assert_rejected(&repos, "facts path absent from repository inventory");
    }
}

#[test]
fn empty_inventory_fails_without_partial_output() {
    for porcelain in ["", "\n", " \t\n\n"] {
        let repos = json!([
            {"repo":"a","porcelain":"worktree /a\nbare\n","facts":{}},
            {"repo":"b","porcelain":porcelain,"facts":{}}
        ]);
        assert_rejected(&repos, "empty porcelain inventory");
    }
}

#[test]
fn lifecycle_labels_do_not_override_commit_or_lock_protections() {
    let f = json!({"idle_hours":192,"clean":true,"merged_into_main":true,"pushed":true,"failed":false,"has_claim":false,"live_run":false,"open_pr":false,"in_use_by_process":false,"in_use_by_unit":false,"is_clone_path":false});
    let mut repos = json!([{
        "repo":"r",
        "porcelain":"worktree /main\nHEAD abc\nbranch refs/heads/trunk\n\nworktree /merged\nHEAD abc\ndetached\n\nworktree /failed\nHEAD abc\ndetached\n\nworktree /abandoned\nHEAD abc\ndetached\n\nworktree /locked\nHEAD abc\ndetached\nlocked owner\n\nworktree /unpushed\nHEAD abc\ndetached\n",
        "facts":{"/main":f,"/merged":f,"/failed":f,"/abandoned":f,"/locked":f,"/unpushed":f}
    }]);
    repos[0]["facts"]["/failed"]["failed"] = true.into();
    repos[0]["facts"]["/failed"]["merged_into_main"] = false.into();
    repos[0]["facts"]["/abandoned"]["merged_into_main"] = false.into();
    repos[0]["facts"]["/unpushed"]["pushed"] = false.into();
    let output = run_report(&repos);
    assert!(output.status.success(), "{:?}", output);
    assert_eq!(
        String::from_utf8(output.stdout).unwrap(),
        concat!(
            "repo\tpath\tbranch\tstate\tstatus\treason\tneeds_backup\n",
            "r\t/abandoned\t-\tabandoned\tprotected\tunmerged-commits\tfalse\n",
            "r\t/failed\t-\tfailed\tprotected\tunmerged-commits\tfalse\n",
            "r\t/locked\t-\tlive\tprotected\tlocked\tfalse\n",
            "r\t/main\trefs/heads/trunk\tlive\tprotected\tmain-worktree\tfalse\n",
            "r\t/merged\t-\tmerged\teligible\tmerged-clean-24h\tfalse\n",
            "r\t/unpushed\t-\tmerged\tprotected\tunpushed-commits\tfalse\n",
        )
    );
}

#[test]
fn report_is_sorted_stable_and_read_only() {
    let dir = tempfile::tempdir().unwrap();
    let sentinel = dir.path().join("keep");
    fs::write(&sentinel, "unchanged").unwrap();
    let f = json!({"idle_hours":192,"clean":false,"merged_into_main":true,"pushed":true,"failed":false,"has_claim":false,"live_run":false,"open_pr":false,"in_use_by_process":false,"in_use_by_unit":false,"is_clone_path":false});
    let mut repos = json!([
        {"repo":"z","porcelain":"worktree /x\nHEAD abc\ndetached\n","facts":{}},
        {"repo":"a","porcelain":"worktree /main\nHEAD def\nbranch refs/heads/trunk\n\nworktree /b\nHEAD abc\ndetached\n\nworktree /a\nHEAD abc\ndetached\n","facts":{"/main":f,"/a":f,"/b":f}}
    ]);
    let original = fs::metadata(dir.path()).unwrap().permissions();
    let mut permissions = original.clone();
    permissions.set_readonly(true);
    fs::set_permissions(dir.path(), permissions).unwrap();
    let before = fs::metadata(dir.path()).unwrap().modified().unwrap();
    let run = |value: &serde_json::Value| {
        let mut child = Command::new(env!("CARGO_BIN_EXE_pg-reap"))
            .arg("report")
            .current_dir(dir.path())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let mut stdin = child.stdin.take().unwrap();
        write!(stdin, "{value}").unwrap();
        drop(stdin);
        child.wait_with_output().unwrap()
    };
    let first = run(&repos);
    repos.as_array_mut().unwrap().reverse();
    repos[0]["porcelain"] =
        json!("worktree /main\nHEAD def\nbranch refs/heads/trunk\n\nworktree /a\nHEAD abc\ndetached\n\nworktree /b\nHEAD abc\ndetached\n");
    let second = run(&repos);
    let after = fs::metadata(dir.path()).unwrap().modified().unwrap();
    let entries = fs::read_dir(dir.path()).unwrap().count();
    let content = fs::read_to_string(&sentinel).unwrap();
    let readonly = fs::metadata(dir.path()).unwrap().permissions().readonly();
    fs::set_permissions(dir.path(), original).unwrap();
    assert!(first.status.success(), "{:?}", first.stderr);
    assert!(second.status.success(), "{:?}", second.stderr);
    assert_eq!(first.stdout, second.stdout);
    assert_eq!(String::from_utf8(first.stdout).unwrap(), "repo\tpath\tbranch\tstate\tstatus\treason\tneeds_backup\na\t/a\t-\tmerged\teligible\tdirty-7d-needs-backup\ttrue\na\t/b\t-\tmerged\teligible\tdirty-7d-needs-backup\ttrue\na\t/main\trefs/heads/trunk\tlive\tprotected\tmain-worktree\tfalse\nz\t/x\t-\tlive\tprotected\tmissing-facts\tfalse\n");
    assert_eq!(
        (before, entries, content.as_str(), readonly),
        (after, 1, "unchanged", true)
    );
    for field in ["has_claim", "pushed", "failed"] {
        let mut incomplete = repos.clone();
        incomplete[0]["facts"]["/a"]
            .as_object_mut()
            .unwrap()
            .remove(field);
        let output = run(&incomplete);
        assert!(!output.status.success());
        assert!(output.stdout.is_empty());
    }
}
