use super::base::Integrated;
use super::*;

fn git_in(dir: &Path, args: &[&str]) -> String {
    output(
        Command::new("git")
            .arg("-C")
            .arg(dir)
            .args([
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
            ])
            .args(args),
    )
    .unwrap()
}

/// A host over a clone whose `origin` has only `main`; `lane/local` exists in the clone alone.
fn local_base_fixture(dir: &Path) -> Host {
    let origin = dir.join("origin.git");
    let repo = dir.join("repo");
    git_in(dir, &["init", "-q", "--bare", "-b", "main", "origin.git"]);
    git_in(
        dir,
        &[
            "clone",
            "-q",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            origin.to_str().unwrap(),
            "repo",
        ],
    );
    git_in(&repo, &["commit", "-q", "--allow-empty", "-m", "base"]);
    git_in(&repo, &["push", "-q", "origin", "HEAD:refs/heads/main"]);
    git_in(&repo, &["checkout", "-q", "-b", "lane/local"]);
    git_in(
        &repo,
        &["commit", "-q", "--allow-empty", "-m", "unpushed lane head"],
    );
    Host {
        repo,
        run_id: "run-local-base".into(),
        cwd: dir.join("worktrees/run-local-base"),
        reviews: dir.join("reviews"),
        record_path: dir.join("record.json"),
        base_ref: "refs/heads/lane/local".into(),
        tool_path: std::env::var("PATH").unwrap(),
        effort: WRITER_EFFORT.into(),
        state: dir.to_path_buf(),
        brief: "brief".into(),
        push_ref: Some("refs/heads/phase/local-result".into()),
        reviews_started: 0,
        review_override: None,
        review_cli: vec!["false".into()],
        python_src: None,
    }
}

#[cfg(unix)]
#[test]
fn host_reaps_a_writer_when_disk_crosses_the_stop_floor() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = local_base_fixture(dir.path());
    host.cwd = host.repo.clone();
    for (name, script) in [
        ("codex", "#!/bin/sh\necho $$ > writer.pid\nexec sleep 4\n"),
        (
            "df",
            "#!/bin/sh\nif [ -s \"$3/writer.pid\" ]; then echo 'x x x 0'; else echo 'x x x 10485760'; fi\n",
        ),
    ] {
        let path = dir.path().join(name);
        pg_worker_core::executable_test_support::write_private_executable(&path, script);
    }
    let path = std::env::var_os("PATH").unwrap();
    host.tool_path = std::env::join_paths(
        std::iter::once(dir.path().to_path_buf()).chain(std::env::split_paths(&path)),
    )
    .unwrap()
    .into_string()
    .unwrap();

    assert_eq!(
        host.execute(None, "brief").unwrap_err(),
        "disk_low: under 2 GB stop floor"
    );
    let pid = fs::read_to_string(host.cwd.join("writer.pid")).unwrap();
    let pid: i32 = pid.trim().parse().unwrap();
    assert_ne!(unsafe { libc::kill(pid, 0) }, 0, "writer was not reaped");
}

#[test]
fn commit_range_counts_a_cherry_pick_and_excludes_base_history() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = local_base_fixture(dir.path());
    host.cwd = host.repo.clone();
    let main = git_in(&host.cwd, &["rev-parse", "main"]);
    let base = git_in(&host.cwd, &["rev-parse", "HEAD"]);
    git_in(&host.cwd, &["checkout", "-qb", "old-lane", &main]);
    git_in(&host.cwd, &["commit", "-qm", "work", "--allow-empty"]);
    let original = git_in(&host.cwd, &["rev-parse", "HEAD"]);
    git_in(&host.cwd, &["checkout", "-q", "lane/local"]);
    git_in(&host.cwd, &["cherry-pick", "--allow-empty", &original]);
    let picked = git_in(&host.cwd, &["rev-parse", "HEAD"]);

    assert_ne!(original, picked);
    assert!(host.has_commits_beyond(&base, &picked).unwrap());
    assert!(!host.has_commits_beyond(&base, &base).unwrap());
    assert!(!host.has_commits_beyond(&base, &main).unwrap());
}

#[test]
fn base_ref_that_exists_only_in_the_host_repo_starts_and_pushes_its_result() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = local_base_fixture(dir.path());
    let local_head = git_in(&host.repo, &["rev-parse", "refs/heads/lane/local"]);
    assert_eq!(host.prepare().unwrap(), local_head);
    assert_eq!(git_in(&host.cwd, &["rev-parse", "HEAD"]), local_head);
    git_in(
        &host.cwd,
        &["commit", "-q", "--allow-empty", "-m", "writer result"],
    );
    let head = git_in(&host.cwd, &["rev-parse", "HEAD"]);
    assert_eq!(
        host.push(&head).unwrap().as_deref(),
        Some("refs/heads/phase/local-result")
    );
    let origin = dir.path().join("origin.git");
    assert_eq!(
        git_in(&origin, &["rev-parse", "refs/heads/phase/local-result"]),
        head
    );
}

#[test]
fn base_ref_on_neither_remote_nor_host_repo_is_refused_by_name_before_a_worktree() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = local_base_fixture(dir.path());
    host.base_ref = "refs/heads/lane/missing".into();
    let error = host.prepare().unwrap_err();
    assert!(error.contains("refs/heads/lane/missing"), "{error}");
    assert!(!host.cwd.exists());
}

#[test]
fn a_different_ref_that_only_contains_the_name_is_never_the_base() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = local_base_fixture(dir.path());
    // Local `refs/heads/refs/heads/missing` would answer `rev-parse refs/heads/missing`.
    git_in(
        &host.repo,
        &[
            "update-ref",
            "refs/heads/refs/heads/missing",
            "refs/heads/lane/local",
        ],
    );
    host.base_ref = "refs/heads/missing".into();
    let error = host.prepare().unwrap_err();
    assert!(error.contains("refs/heads/missing"), "{error}");
    assert!(!host.cwd.exists());
}

#[test]
fn a_longer_remote_ref_does_not_hide_the_host_local_base() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = local_base_fixture(dir.path());
    // `ls-remote origin refs/heads/lane/local` also lists `refs/heads/other/refs/heads/lane/local`.
    git_in(
        &host.repo,
        &[
            "push",
            "-q",
            "origin",
            "refs/heads/main:refs/heads/other/refs/heads/lane/local",
        ],
    );
    let local_head = git_in(&host.repo, &["rev-parse", "refs/heads/lane/local"]);
    assert_eq!(host.prepare().unwrap(), local_head);
}

#[test]
fn a_base_ref_ending_in_a_unicode_space_is_matched_byte_for_byte() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = local_base_fixture(dir.path());
    let name = "refs/heads/lane/nbsp\u{a0}";
    git_in(&host.repo, &["update-ref", name, "refs/heads/lane/local"]);
    host.base_ref = name.into();
    let local_head = git_in(&host.repo, &["rev-parse", "refs/heads/lane/local"]);
    assert_eq!(host.prepare().unwrap(), local_head);
}

#[test]
fn native_creation_waits_for_the_reaper_lock() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = local_base_fixture(dir.path());
    let held = pg_worker_core::worktree::mutation_lock(&host.repo).unwrap();
    let error = host.prepare().unwrap_err();
    assert!(
        error.contains("lock not acquired before the deadline"),
        "{error}"
    );
    assert!(!host.cwd.exists());
    drop(held);
    host.prepare().unwrap();
    assert!(host.cwd.exists());
}

#[test]
fn native_review_base_gate_binds_the_writer_record_to_one_head() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = local_base_fixture(dir.path());
    host.base_ref = "refs/heads/main".into();
    let base = host.prepare().unwrap();
    git_in(
        &host.cwd,
        &["commit", "-qm", "writer result", "--allow-empty"],
    );
    let head = git_in(&host.cwd, &["rev-parse", "HEAD"]);
    git_in(&host.repo, &["checkout", "-q", "main"]);
    git_in(&host.repo, &["commit", "-qm", "train", "--allow-empty"]);
    git_in(&host.repo, &["push", "-q", "origin", "main"]);
    let main = git_in(&host.repo, &["rev-parse", "HEAD"]);

    let src = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../src");
    let output = crate::test_python::command()
        .env("PYTHONPATH", src)
        .args(["-c", r#"
import asyncio, sys
from types import SimpleNamespace
from plangraph.spawner.base_fresh import admit_base

repo, base, head, main = sys.argv[1:]
writer = dict(run_id='run-fixture', run_kind='native-codex', role='implement', status='running',
              base_admitted=True, repo=repo, base_sha=base, final_head=head,
              fast_checks=dict(head=head, status='passed'),
              review_base=dict(head=head, base=base, main=base, observed_main=main, mode='conflict'))
service = SimpleNamespace(result=lambda _: writer, _save=lambda _: None)
card = dict(review_workspace=True, native_writer_run='run-fixture',
            diff_ref=dict(repo=repo, base=base, head=head))
def check(card, launch=False):
    record = dict(role='review', cwd=repo)
    return asyncio.run(admit_base(service, record, card, launch=launch)), record
for launch in (False, True):
    assert check(dict(card, native_writer_run=None), launch)[1]['reason'] == 'base_stale: behind=1'
    ok, row = check(card, launch)
    assert ok, row
    assert row['base_sha'] == base and row['checked_base_sha'] == head, row
    assert row['review_base'] == writer['review_base'], row
    assert check(dict(card, diff_ref=dict(card['diff_ref'], head=base)), launch)[0] is False
    assert check(dict(card, diff_ref=dict(card['diff_ref'], base=main)), launch)[0] is False
    assert check(dict(card, native_writer_run='run-wrong'), launch)[0] is False
    writer['fast_checks']['status'] = 'failed'
    assert check(card, launch)[0] is False
    writer['fast_checks']['status'] = 'passed'
"#])
        .arg(&host.repo)
        .args([&base, &head, &main])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[path = "native_writer_moving_main_tests.rs"]
mod moving_main;

/// A part (lane) of two commits whose first patch main already holds under another commit id, on a
/// clone whose main moved past the part's base; returns (host, old main, new main).
fn part_with_upstream_first_patch(dir: &Path) -> (Host, String, String) {
    let mut host = local_base_fixture(dir);
    host.cwd = host.repo.clone();
    let old_main = git_in(&host.cwd, &["rev-parse", "main"]);
    git_in(&host.cwd, &["checkout", "-q", "-b", "part", &old_main]);
    fs::write(host.cwd.join("a.txt"), "a\n").unwrap();
    git_in(&host.cwd, &["add", "a.txt"]);
    git_in(&host.cwd, &["commit", "-qm", "first"]);
    let first = git_in(&host.cwd, &["rev-parse", "HEAD"]);
    fs::write(host.cwd.join("b.txt"), "b\n").unwrap();
    git_in(&host.cwd, &["add", "b.txt"]);
    git_in(&host.cwd, &["commit", "-qm", "second"]);
    git_in(&host.cwd, &["checkout", "-q", "main"]);
    git_in(&host.cwd, &["cherry-pick", &first]);
    let new_main = git_in(&host.cwd, &["rev-parse", "HEAD"]);
    git_in(&host.cwd, &["checkout", "-q", "part"]);
    (host, old_main, new_main)
}

#[test]
fn rebase_drops_a_commit_whose_patch_is_already_on_main() {
    let dir = tempfile::tempdir().unwrap();
    let (host, _old_main, new_main) = part_with_upstream_first_patch(dir.path());
    assert_eq!(host.rebase(&new_main).unwrap(), Integrated::Done);
    let own = git_in(
        &host.cwd,
        &["log", "--format=%s", &format!("{new_main}..HEAD")],
    );
    assert_eq!(
        own, "second",
        "the upstream patch is dropped, not kept empty"
    );
    assert_eq!(git_in(&host.cwd, &["rev-parse", "HEAD^"]), new_main);
}

#[test]
fn rebase_names_a_conflict_only_when_a_path_is_unmerged() {
    let dir = tempfile::tempdir().unwrap();
    let (host, _, _) = part_with_upstream_first_patch(dir.path());
    // The part's second commit now clashes with a different b.txt on main.
    git_in(&host.cwd, &["checkout", "-q", "main"]);
    fs::write(host.cwd.join("b.txt"), "other\n").unwrap();
    git_in(&host.cwd, &["add", "b.txt"]);
    git_in(&host.cwd, &["commit", "-qm", "main b"]);
    let main = git_in(&host.cwd, &["rev-parse", "HEAD"]);
    git_in(&host.cwd, &["checkout", "-q", "part"]);
    let head = git_in(&host.cwd, &["rev-parse", "HEAD"]);
    assert_eq!(
        host.rebase(&main).unwrap(),
        Integrated::Conflict(vec!["b.txt".to_owned()])
    );
    assert_eq!(git_in(&host.cwd, &["rev-parse", "HEAD"]), head);
    assert!(git_in(&host.cwd, &["status", "--porcelain"]).is_empty());
}

#[test]
fn rebase_failure_without_an_unmerged_path_is_an_error_not_a_conflict() {
    let dir = tempfile::tempdir().unwrap();
    let (host, _old_main, _) = part_with_upstream_first_patch(dir.path());
    let head = git_in(&host.cwd, &["rev-parse", "HEAD"]);
    let missing = "0".repeat(40);
    assert!(host.rebase(&missing).is_err());
    assert_eq!(git_in(&host.cwd, &["rev-parse", "HEAD"]), head);
}

#[test]
fn review_base_stays_rebased_when_every_commit_is_already_on_main() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = local_base_fixture(dir.path());
    host.base_ref = "refs/heads/main".into();
    host.record_path = dir.path().join("runs/run-local-base/record.json");
    fs::create_dir_all(host.record_path.parent().unwrap()).unwrap();
    host.prepare().unwrap();
    fs::write(host.cwd.join("a.txt"), "a\n").unwrap();
    git_in(&host.cwd, &["add", "a.txt"]);
    git_in(&host.cwd, &["commit", "-qm", "part"]);
    let head = git_in(&host.cwd, &["rev-parse", "HEAD"]);
    // Main takes the same patch under another commit id.
    git_in(&host.repo, &["checkout", "-q", "main"]);
    git_in(&host.repo, &["commit", "-qm", "train", "--allow-empty"]);
    git_in(&host.repo, &["cherry-pick", &head]);
    git_in(&host.repo, &["push", "-q", "origin", "main"]);
    let main = git_in(&host.repo, &["rev-parse", "main"]);
    let review_base = host.prepare_review_base(&head).unwrap();
    assert_eq!(review_base.mode, "rebased");
    assert!(review_base.conflict_files.is_empty());
    assert_eq!(review_base.base, main);
    assert_eq!(git_in(&host.cwd, &["rev-parse", "HEAD"]), main);
}

/// Commit `content` as `name` on the checked-out branch of the host's clone.
fn commit_file(host: &Host, name: &str, content: &str, message: &str) -> String {
    fs::write(host.cwd.join(name), content).unwrap();
    git_in(&host.cwd, &["add", name]);
    git_in(&host.cwd, &["commit", "-qm", message]);
    git_in(&host.cwd, &["rev-parse", "HEAD"])
}

#[test]
fn rebase_drops_an_upstream_patch_that_main_then_edited_next_to() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = local_base_fixture(dir.path());
    host.cwd = host.repo.clone();
    git_in(&host.cwd, &["checkout", "-q", "main"]);
    commit_file(&host, "f.txt", "1\n2\n3\n4\n", "file");
    git_in(&host.cwd, &["checkout", "-q", "-b", "part"]);
    let first = commit_file(&host, "f.txt", "1\n2x\n3\n4\n", "first");
    commit_file(&host, "b.txt", "b\n", "second");
    git_in(&host.cwd, &["checkout", "-q", "main"]);
    git_in(&host.cwd, &["cherry-pick", &first]);
    // Main then edits the line next to the upstream patch: replaying the patch alone would clash.
    let new_main = commit_file(&host, "f.txt", "1\n2x\n3y\n4\n", "main edit");
    git_in(&host.cwd, &["checkout", "-q", "part"]);
    assert_eq!(host.rebase(&new_main).unwrap(), Integrated::Done);
    let own = git_in(
        &host.cwd,
        &["log", "--format=%s", &format!("{new_main}..HEAD")],
    );
    assert_eq!(own, "second");
    assert_eq!(
        fs::read_to_string(host.cwd.join("f.txt")).unwrap(),
        "1\n2x\n3y\n4\n"
    );
}

#[test]
fn conflict_files_are_the_exact_paths_even_when_git_quotes_them() {
    let dir = tempfile::tempdir().unwrap();
    let (host, _old_main, _) = part_with_upstream_first_patch(dir.path());
    let name = "caf\u{e9} \u{2192}.txt";
    commit_file(&host, name, "part\n", "third");
    git_in(&host.cwd, &["checkout", "-q", "main"]);
    let main = commit_file(&host, name, "main\n", "main third");
    git_in(&host.cwd, &["checkout", "-q", "part"]);
    assert_eq!(
        host.rebase(&main).unwrap(),
        Integrated::Conflict(vec![name.to_owned()])
    );
}

fn rebase_state_exists(host: &Host) -> bool {
    let path = git_in(
        &host.cwd,
        &[
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "rebase-merge",
        ],
    );
    Path::new(&path).exists()
}

#[test]
fn a_failed_rebase_with_state_but_no_unmerged_path_is_aborted_and_an_error() {
    let dir = tempfile::tempdir().unwrap();
    let (host, old_main, _) = part_with_upstream_first_patch(dir.path());
    let head = git_in(&host.cwd, &["rev-parse", "HEAD"]);
    // An `exec false` step stops the rebase with its state in place and nothing unmerged.
    let result = host.git(&[
        "-c",
        "sequence.editor=printf 'exec false\\n' >",
        "rebase",
        "-i",
        &old_main,
    ]);
    assert!(result.is_err());
    assert!(rebase_state_exists(&host));
    let outcome = host.finish_integration(result, "rebase-merge", &["rebase", "--abort"]);
    assert!(outcome.is_err(), "{outcome:?}");
    assert!(!rebase_state_exists(&host), "the rebase is aborted");
    assert_eq!(git_in(&host.cwd, &["rev-parse", "HEAD"]), head);
    assert!(git_in(&host.cwd, &["status", "--porcelain"]).is_empty());
}
