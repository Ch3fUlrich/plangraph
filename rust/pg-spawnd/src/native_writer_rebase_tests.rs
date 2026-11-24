use super::super::base::{lossless_path, Integrated};
use super::*;

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

#[test]
fn a_patch_main_holds_only_up_to_whitespace_is_not_dropped() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = local_base_fixture(dir.path());
    host.cwd = host.repo.clone();
    // A user's log format must not hide the patch ids.
    git_in(&host.cwd, &["config", "format.pretty", "%s"]);
    git_in(&host.cwd, &["checkout", "-q", "main"]);
    commit_file(&host, "g.txt", "x\n", "file");
    git_in(&host.cwd, &["checkout", "-q", "-b", "part"]);
    commit_file(&host, "g.txt", "x\ns = \"a b\"\n", "part");
    let head = git_in(&host.cwd, &["rev-parse", "HEAD"]);
    git_in(&host.cwd, &["checkout", "-q", "main"]);
    let main = commit_file(&host, "g.txt", "x\ns = \"ab\"\n", "main");
    git_in(&host.cwd, &["checkout", "-q", "part"]);
    let error = host.rebase(&main).unwrap_err();
    assert!(error.contains("same change at the same place"), "{error}");
    assert_eq!(git_in(&host.cwd, &["rev-parse", "HEAD"]), head);
    assert!(git_in(&host.cwd, &["status", "--porcelain"]).is_empty());
}

#[test]
fn conflict_paths_stay_distinct_when_their_bytes_are_not_utf8() {
    use std::os::unix::ffi::OsStrExt;
    let dir = tempfile::tempdir().unwrap();
    let (host, _, _) = part_with_upstream_first_patch(dir.path());
    let names: [&[u8]; 2] = [b"caf\xfe.txt", b"caf\xff.txt"];
    let write = |content: &str| {
        for name in names {
            fs::write(host.cwd.join(std::ffi::OsStr::from_bytes(name)), content).unwrap();
        }
        git_in(&host.cwd, &["add", "-A"]);
        git_in(&host.cwd, &["commit", "-qm", content]);
    };
    write("part\n");
    git_in(&host.cwd, &["checkout", "-q", "main"]);
    write("main\n");
    let main = git_in(&host.cwd, &["rev-parse", "HEAD"]);
    git_in(&host.cwd, &["checkout", "-q", "part"]);
    assert_eq!(
        host.rebase(&main).unwrap(),
        Integrated::Conflict(vec!["caf\\xfe.txt".to_owned(), "caf\\xff.txt".to_owned()])
    );
}

#[test]
fn lossless_path_never_gives_one_text_for_two_paths() {
    assert_eq!(lossless_path("a/b é.txt".as_bytes()), "a/b é.txt");
    assert_eq!(lossless_path(b"x\xfe"), "x\\xfe");
    assert_eq!(lossless_path(b"x\xe2\x82"), "x\\xe2\\x82");
    assert_eq!(lossless_path(b"x\\xfe"), "x\\\\xfe");
    assert_ne!(lossless_path(b"x\xfe"), lossless_path(b"x\\xfe"));
}

/// Seven lines repeated three times: an edit of the fourth line has the same patch id in each block.
const BLOCKS: &str = "a\nb\nc\nd\ne\nf\ng\n--\na\nb\nc\nd\ne\nf\ng\n--\na\nb\nc\nd\ne\nf\ng\n";

#[test]
fn the_same_edit_in_another_repeated_block_is_not_an_upstream_patch() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = local_base_fixture(dir.path());
    host.cwd = host.repo.clone();
    git_in(&host.cwd, &["checkout", "-q", "main"]);
    commit_file(&host, "r.txt", BLOCKS, "blocks");
    git_in(&host.cwd, &["checkout", "-q", "-b", "part"]);
    let first_block = BLOCKS.replacen("d\n", "D\n", 1);
    commit_file(&host, "r.txt", &first_block, "part edits block one");
    let head = git_in(&host.cwd, &["rev-parse", "HEAD"]);
    git_in(&host.cwd, &["checkout", "-q", "main"]);
    let (one, rest) = BLOCKS.split_at(BLOCKS.find("--\n").unwrap() + 3);
    let second_block = format!("{one}{}", rest.replacen("d\n", "D\n", 1));
    let main = commit_file(&host, "r.txt", &second_block, "main edits block two");
    git_in(&host.cwd, &["checkout", "-q", "part"]);
    let error = host.rebase(&main).unwrap_err();
    assert!(error.contains("same change at the same place"), "{error}");
    assert_eq!(git_in(&host.cwd, &["rev-parse", "HEAD"]), head);
}

#[test]
fn an_upstream_patch_is_still_dropped_under_a_user_log_format() {
    let dir = tempfile::tempdir().unwrap();
    let (host, _, new_main) = part_with_upstream_first_patch(dir.path());
    git_in(&host.cwd, &["config", "format.pretty", "%s"]);
    assert_eq!(host.rebase(&new_main).unwrap(), Integrated::Done);
    assert_eq!(git_in(&host.cwd, &["rev-parse", "HEAD^"]), new_main);
}
