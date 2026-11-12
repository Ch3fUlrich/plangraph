use serde_json::json;
use std::process::{Command, Stdio};
use std::{fs, io::Write};

#[test]
fn report_is_sorted_stable_and_read_only() {
    let dir = tempfile::tempdir().unwrap();
    let sentinel = dir.path().join("keep");
    fs::write(&sentinel, "unchanged").unwrap();
    let f = json!({"idle_hours":192,"clean":false,"merged_into_main":false,"has_claim":false,"live_run":false,"open_pr":false,"in_use_by_process":false,"in_use_by_unit":false,"is_clone_path":false});
    let mut repos = json!([
        {"repo":"z","porcelain":"worktree /x\nHEAD abc\ndetached\n","facts":{}},
        {"repo":"a","porcelain":"worktree /b\nHEAD abc\ndetached\n\nworktree /a\nHEAD abc\ndetached\n","facts":{"/a":f,"/b":f}}
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
        json!("worktree /a\nHEAD abc\ndetached\n\nworktree /b\nHEAD abc\ndetached\n");
    let second = run(&repos);
    let after = fs::metadata(dir.path()).unwrap().modified().unwrap();
    let entries = fs::read_dir(dir.path()).unwrap().count();
    let content = fs::read_to_string(&sentinel).unwrap();
    let readonly = fs::metadata(dir.path()).unwrap().permissions().readonly();
    fs::set_permissions(dir.path(), original).unwrap();
    assert!(first.status.success(), "{:?}", first.stderr);
    assert!(second.status.success(), "{:?}", second.stderr);
    assert_eq!(first.stdout, second.stdout);
    assert_eq!(String::from_utf8(first.stdout).unwrap(), "repo\tpath\tbranch\tstatus\treason\tneeds_backup\na\t/a\t-\teligible\tdirty-7d-needs-backup\ttrue\na\t/b\t-\teligible\tdirty-7d-needs-backup\ttrue\nz\t/x\t-\tprotected\tmissing-facts\tfalse\n");
    assert_eq!(
        (before, entries, content.as_str(), readonly),
        (after, 1, "unchanged", true)
    );
    let facts = repos[0]["facts"]["/a"].as_object_mut().unwrap();
    facts.remove("has_claim");
    assert!(!run(&repos).status.success());
    assert!(run(&repos).stdout.is_empty());
}
