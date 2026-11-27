//! A main checkout's own `rust/target` is outside every cap: report it, remove it once idle.
use crate::disk_hygiene::{scan, Guards, Report};
use crate::shared_target::CACHEDIR_SIGNATURE;
use std::{
    fs, io,
    path::Path,
    time::{Duration, SystemTime},
};

#[cfg(target_os = "linux")]
pub(crate) fn file_identity(meta: &fs::Metadata) -> (u64, u64) {
    use std::os::unix::fs::MetadataExt;
    (meta.dev(), meta.ino())
}

#[cfg(target_os = "linux")]
pub(crate) fn hygiene_artifact(
    item: &crate::shared_target::Candidate,
    end: std::time::Instant,
) -> io::Result<Option<&'static str>> {
    use crate::shared_target;
    if !item.variant {
        let found = shared_target::debug_artifacts(&item.scope)?;
        return Ok((!found.contains(&item.path)).then_some("unrecognized"));
    }
    match shared_target::candidates_until(&item.scope, SystemTime::now(), || {
        std::time::Instant::now() < end
    }) {
        Ok(found) => {
            Ok((!found.iter().any(|(path, _)| path == &item.path)).then_some("unrecognized"))
        }
        Err(error) if error.kind() == io::ErrorKind::TimedOut => Ok(Some("run_budget")),
        Err(error) => Err(error),
    }
}
/// Cargo's own markers, never the directory name: its CACHEDIR.TAG or the `.rustc_info.json`
/// beside a profile directory.
pub fn identified(target: &Path) -> bool {
    let tag = target.join("CACHEDIR.TAG");
    fs::symlink_metadata(&tag).is_ok_and(|m| m.is_file() && m.len() <= 1 << 20)
        && fs::read(&tag).is_ok_and(|b| b.starts_with(CACHEDIR_SIGNATURE))
        || crate::private_targets::cargo_target(target)
}

pub(crate) fn git_until(
    tree: &Path,
    args: &[&str],
    deadline: std::time::Instant,
) -> io::Result<Vec<u8>> {
    let remaining = deadline
        .checked_duration_since(std::time::Instant::now())
        .ok_or_else(|| io::Error::from(io::ErrorKind::TimedOut))?;
    crate::host_command::output(
        std::process::Command::new("git")
            .arg("-C")
            .arg(tree)
            .args(args),
        remaining,
    )
}

/// Check that a worktree's Cargo target contains only ignored, untracked files.
pub fn ignored_rust_target(tree: &Path, deadline: std::time::Instant) -> io::Result<bool> {
    let git = |args: &[&str]| git_until(tree, args, deadline);
    let ignored = git(&[
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "--directory",
        "-z",
        "--",
        "rust/target/",
    ])?;
    Ok(ignored == b"rust/target/\0"
        && git(&[
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            "rust/target/",
        ])?
        .is_empty())
}

/// Track source metadata within one directory without following marker symlinks.
#[derive(Default)]
pub(crate) struct GitMarkers(u8);

impl GitMarkers {
    pub(crate) fn observe(&mut self, name: &std::ffi::OsStr, kind: fs::FileType) {
        let marker_dir = kind.is_dir() || kind.is_symlink();
        self.0 |= match name.to_str() {
            Some(".git") => 8,
            Some("HEAD") if !kind.is_dir() => 1,
            Some("objects") if marker_dir => 2,
            Some("refs") if marker_dir => 4,
            _ => 0,
        };
    }

    pub(crate) fn is_repository(&self) -> bool {
        self.0 >= 7
    }
}

/// A Git repository below the target is someone's source tree, never a build cache, whatever markers
/// sit beside it: a `.git` directory or file (a repository or linked worktree) or a bare repository
/// (a `HEAD` file beside `objects` and `refs` directories, which may be symlinks) at any depth.
pub(crate) fn holds_git_repository(target: &Path) -> io::Result<bool> {
    let mut pending = vec![target.to_path_buf()];
    while let Some(directory) = pending.pop() {
        let mut markers = GitMarkers::default();
        for entry in fs::read_dir(&directory)? {
            let entry = entry?;
            let kind = entry.file_type()?;
            // A marker directory may be a symlink (a shared object store, a linked refs directory):
            // it counts as present and is never followed.
            markers.observe(&entry.file_name(), kind);
            if markers.is_repository() {
                return Ok(true);
            }
            if kind.is_dir() {
                pending.push(entry.path());
            }
        }
    }
    Ok(false)
}

/// A pin or live path that is the target or lies inside it refuses the sweep. One that names an
/// ancestor does not: a unit `WorkingDirectory=` at the checkout root pins the whole checkout on
/// the hosts this sweep exists for, and a process working inside the target is checked separately.
fn names_target(guards: &Guards, target: &Path) -> bool {
    Guards::protected_name(target)
        || guards.pinned.iter().chain(&guards.live).any(|path| {
            path.canonicalize()
                .unwrap_or_else(|_| path.clone())
                .starts_with(target)
        })
}

/// A target nothing wrote to for this long belongs to no running build.
pub const IDLE_AFTER: Duration = Duration::from_secs(3600);

/// Report `<repo>/rust/target` and, when `apply` and it has been idle for [`IDLE_AFTER`]
/// with no cargo running, remove it. Builds belong on the shared, capped target.
pub fn sweep_main_checkout_target(
    repo: &Path,
    guards: &Guards,
    now: SystemTime,
    apply: bool,
    report: &mut Report,
    processes: impl FnOnce() -> io::Result<(Vec<std::path::PathBuf>, bool)>,
) -> io::Result<()> {
    sweep_owned(
        repo,
        guards,
        now,
        apply,
        report,
        processes,
        &crate::private_targets::owned,
    )
}

fn sweep_owned(
    repo: &Path,
    guards: &Guards,
    now: SystemTime,
    apply: bool,
    report: &mut Report,
    processes: impl FnOnce() -> io::Result<(Vec<std::path::PathBuf>, bool)>,
    owned: &dyn Fn(&Path) -> io::Result<bool>,
) -> io::Result<()> {
    let target = repo.join("rust/target");
    let meta = match fs::symlink_metadata(&target) {
        Ok(meta) => meta,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(e) => return Err(e),
    };
    // Only a plain directory inside a main checkout (`.git` is a directory, not a worktree file).
    if !meta.is_dir() || !repo.join(".git").is_dir() {
        return Ok(());
    }
    if !identified(&target) {
        report.skipped.push(format!(
            "{}: not an identified cargo target; not touched",
            target.display()
        ));
        return Ok(());
    }
    // Same refusals as every other reclamation: pinned or live paths and foreign-owned targets stay.
    if !owned(&target)? {
        report.skipped.push(format!(
            "{}: main checkout target owned by another user; not touched",
            target.display()
        ));
        return Ok(());
    }
    // The checkout may be reached through a symlinked ancestor, so paths are compared canonically.
    let canonical = target.canonicalize()?;
    // A symlink at `rust` (or any component below the checkout) would lead the removal elsewhere.
    if canonical != repo.canonicalize()?.join("rust/target") {
        report.skipped.push(format!(
            "{}: target resolves outside the checkout; not touched",
            target.display()
        ));
        return Ok(());
    }
    if names_target(guards, &canonical) {
        report.skipped.push(format!(
            "{}: main checkout target pinned or live; not touched",
            target.display()
        ));
        return Ok(());
    }
    // The sibling lease a build holds shared is taken after the pin check (a pinned target is never
    // touched, not even its lease file) and before anything below the target is read, and held through
    // the scan and the removal.
    let Some(_lease) = crate::cargo_cache::try_exclusive_lease(&canonical)? else {
        report.skipped.push(format!(
            "{}: main checkout target cache lease held; not touched",
            target.display()
        ));
        return Ok(());
    };
    // Only an ignored, untracked directory is a build cache: a tracked file or an unignored one
    // beside the cargo marker is source and never goes with a recursive removal.
    let ignored = crate::disk_hygiene::git(
        repo,
        &[
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--directory",
            "--",
            "rust/target/",
        ],
    )?;
    if !crate::disk_hygiene::git(repo, &["ls-files", "--", "rust/target"])?.is_empty()
        || ignored.trim() != "rust/target/"
    {
        report.skipped.push(format!(
            "{}: main checkout target holds tracked or unignored files; not touched",
            target.display()
        ));
        return Ok(());
    }
    if holds_git_repository(&canonical)? {
        report.skipped.push(format!(
            "{}: main checkout target holds a git repository; not touched",
            target.display()
        ));
        return Ok(());
    }
    let (bytes, newest) = scan(&target)?;
    let idle = now.duration_since(newest).unwrap_or_default();
    report.actions.push(format!(
        "main_checkout_target {} bytes={bytes} idle_secs={}",
        target.display(),
        idle.as_secs()
    ));
    if idle <= IDLE_AFTER {
        report.skipped.push(format!(
            "{}: main checkout target written within the hour",
            target.display()
        ));
        return Ok(());
    }
    let (cwds, building) = processes()?;
    if building
        || cwds
            .iter()
            .chain(&guards.live)
            .any(|cwd| cwd.starts_with(&target) || cwd.starts_with(&canonical))
    {
        report.skipped.push(format!(
            "{}: main checkout target in use by a running build",
            target.display()
        ));
        return Ok(());
    }
    if apply {
        fs::remove_dir_all(&canonical)?;
        report.freed_bytes = report.freed_bytes.saturating_add(bytes);
    }
    report.actions.push(format!(
        "{} {} bytes={bytes}",
        if apply { "removed" } else { "would-remove" },
        target.display()
    ));
    Ok(())
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    fn git(repo: &Path, args: &[&str]) {
        let status = std::process::Command::new("git")
            .arg("-C")
            .arg(repo)
            .args(args)
            .status()
            .unwrap();
        assert!(status.success(), "git {args:?}");
    }

    fn fixture(age_secs: u64) -> (tempfile::TempDir, std::path::PathBuf) {
        let tmp = tempfile::tempdir().unwrap();
        let repo = tmp.path().join("proj");
        fs::create_dir_all(&repo).unwrap();
        git(&repo, &["init", "-q"]);
        fs::write(repo.join(".gitignore"), "rust/target/\n").unwrap();
        let debug = repo.join("rust/target/debug");
        fs::create_dir_all(&debug).unwrap();
        fs::write(debug.join("blob"), [0u8; 64]).unwrap();
        fs::write(
            repo.join("rust/target/CACHEDIR.TAG"),
            b"Signature: 8a477f597d28d172789f06886806bc55\n# cargo",
        )
        .unwrap();
        let old = SystemTime::now() - Duration::from_secs(age_secs);
        for p in [
            debug.join("blob"),
            repo.join("rust/target/CACHEDIR.TAG"),
            debug.clone(),
            repo.join("rust/target"),
        ] {
            fs::File::open(p).unwrap().set_modified(old).unwrap();
        }
        (tmp, repo)
    }

    fn run(repo: &Path, apply: bool, building: bool) -> Report {
        let mut report = Report::default();
        sweep_main_checkout_target(
            repo,
            &Guards::default(),
            SystemTime::now(),
            apply,
            &mut report,
            || Ok((vec![], building)),
        )
        .unwrap();
        report
    }

    #[test]
    fn idle_target_over_an_hour_is_reported_and_removed() {
        let (_tmp, repo) = fixture(7200);
        let dry = run(&repo, false, false);
        assert!(dry.actions.iter().any(|a| a.starts_with("would-remove")));
        assert!(repo.join("rust/target").exists());
        let applied = run(&repo, true, false);
        assert_eq!(applied.freed_bytes, 64 + 51); // blob + CACHEDIR.TAG
        assert!(!repo.join("rust/target").exists());
        assert!(repo.join(".git").exists());
    }

    #[test]
    fn a_pin_naming_the_target_or_inside_it_keeps_it_but_a_checkout_pin_does_not() {
        let (_tmp, repo) = fixture(7200);
        let target = repo.join("rust/target").canonicalize().unwrap();
        let run_with = |guards: &Guards| {
            let mut report = Report::default();
            sweep_main_checkout_target(&repo, guards, SystemTime::now(), true, &mut report, || {
                Ok((vec![], false))
            })
            .unwrap();
            report
        };
        for guards in [
            Guards {
                pinned: vec![target.clone()],
                live: vec![],
            },
            Guards {
                pinned: vec![],
                live: vec![target.join("debug")],
            },
        ] {
            let report = run_with(&guards);
            assert_eq!(report.freed_bytes, 0);
            assert!(repo.join("rust/target/debug/blob").exists());
            assert!(report.skipped[0].contains("pinned or live"));
        }
        // A unit's WorkingDirectory at the checkout root, or any ancestor, pins the whole checkout.
        let ancestors = Guards {
            pinned: vec![
                repo.canonicalize().unwrap(),
                repo.parent().unwrap().to_path_buf(),
            ],
            live: vec![repo.canonicalize().unwrap()],
        };
        assert!(run_with(&ancestors).freed_bytes > 0);
        assert!(!repo.join("rust/target").exists());
    }

    #[test]
    fn a_checkout_reached_through_a_symlinked_ancestor_is_still_swept() {
        let (tmp, repo) = fixture(7200);
        let link = tmp.path().join("data-link");
        std::os::unix::fs::symlink(tmp.path(), &link).unwrap();
        let through_link = link.join("proj");
        let report = run(&through_link, true, false);
        assert!(report.freed_bytes > 0, "{:?}", report.skipped);
        assert!(!repo.join("rust/target").exists());
        // A relative checkout path works the same way.
        let (_tmp, repo) = fixture(7200);
        let relative = std::env::current_dir().unwrap();
        let relative = pathdiff(&repo, &relative);
        assert!(run(&relative, true, false).freed_bytes > 0);
        assert!(!repo.join("rust/target").exists());
    }

    fn pathdiff(path: &Path, base: &Path) -> std::path::PathBuf {
        let (path, base) = (
            path.components().collect::<Vec<_>>(),
            base.components().collect::<Vec<_>>(),
        );
        let common = path.iter().zip(&base).take_while(|(a, b)| a == b).count();
        let mut out = std::path::PathBuf::new();
        for _ in common..base.len() {
            out.push("..");
        }
        for part in &path[common..] {
            out.push(part);
        }
        out
    }

    #[test]
    fn a_symlink_below_the_checkout_never_leads_the_sweep_elsewhere() {
        let (tmp, repo) = fixture(7200);
        let outside = tmp.path().join("shared-cache");
        fs::rename(repo.join("rust"), &outside).unwrap();
        std::os::unix::fs::symlink(&outside, repo.join("rust")).unwrap();
        let blob = outside.join("target/debug/blob");
        assert!(blob.exists());
        for apply in [false, true] {
            let report = run(&repo, apply, false);
            assert_eq!(report.freed_bytes, 0);
            assert!(report.skipped[0].contains("resolves outside the checkout"));
            assert!(!report.actions.iter().any(|a| a.contains("would-remove")));
            assert!(blob.exists());
        }
        // `rust/target` itself linked elsewhere is no plain directory either.
        let (tmp, repo) = fixture(7200);
        let outside = tmp.path().join("elsewhere");
        fs::rename(repo.join("rust/target"), &outside).unwrap();
        std::os::unix::fs::symlink(&outside, repo.join("rust/target")).unwrap();
        assert_eq!(run(&repo, true, false).freed_bytes, 0);
        assert!(outside.join("debug/blob").exists());
    }

    #[test]
    fn tracked_or_unignored_files_beside_the_cargo_marker_keep_the_directory() {
        // A tracked source file whose directory also holds a valid cargo marker.
        let (_tmp, repo) = fixture(7200);
        fs::write(repo.join("rust/target/lib.rs"), b"fn main() {}").unwrap();
        git(&repo, &["add", "-f", "rust/target/lib.rs"]);
        let report = run(&repo, true, false);
        assert_eq!(report.freed_bytes, 0);
        assert!(repo.join("rust/target/lib.rs").exists());
        assert!(report.skipped[0].contains("tracked or unignored"));
        // Without an ignore rule the directory is no cache either, and local edits stay.
        let (_tmp, repo) = fixture(7200);
        fs::remove_file(repo.join(".gitignore")).unwrap();
        assert_eq!(run(&repo, true, false).freed_bytes, 0);
        assert!(repo.join("rust/target/debug/blob").exists());
        // An unignored file inside an otherwise ignored target is also kept.
        let (_tmp, repo) = fixture(7200);
        fs::write(repo.join(".gitignore"), "rust/target/debug/\n").unwrap();
        assert_eq!(run(&repo, true, false).freed_bytes, 0);
    }

    fn age_tree(path: &Path, age_secs: u64) {
        let old = SystemTime::now() - Duration::from_secs(age_secs);
        let mut pending = vec![path.to_path_buf()];
        while let Some(entry) = pending.pop() {
            if fs::symlink_metadata(&entry).unwrap().is_dir() {
                pending.extend(fs::read_dir(&entry).unwrap().map(|e| e.unwrap().path()));
            }
            if !fs::symlink_metadata(&entry).unwrap().is_symlink() {
                fs::File::open(&entry).unwrap().set_modified(old).unwrap();
            }
        }
    }

    fn commit(repo: &Path) {
        git(
            repo,
            &[
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.invalid",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "init",
            ],
        );
    }

    #[test]
    fn a_git_repository_below_the_target_keeps_the_directory() {
        // A scratch checkout nested in the ignored directory, its tracked file edited locally.
        let (_tmp, repo) = fixture(7200);
        let nested = repo.join("rust/target/scratch");
        fs::create_dir_all(&nested).unwrap();
        git(&nested, &["init", "-q"]);
        fs::write(nested.join("src.rs"), b"edited").unwrap();
        age_tree(&repo.join("rust/target"), 7200);
        let report = run(&repo, true, false);
        assert_eq!(report.freed_bytes, 0);
        assert!(nested.join("src.rs").exists() && nested.join(".git").exists());
        assert!(report.skipped[0].contains("git repository"));
    }

    #[test]
    fn a_linked_worktree_or_bare_repository_below_the_target_keeps_the_directory() {
        // Without the detection the aged target is removed: both would then vanish.
        let (_tmp, repo) = fixture(7200);
        commit(&repo);
        let linked = repo.join("rust/target/linked");
        git(
            &repo,
            &[
                "worktree",
                "add",
                "-q",
                "--detach",
                linked.to_str().unwrap(),
            ],
        );
        assert!(linked.join(".git").is_file());
        age_tree(&repo.join("rust/target"), 7200);
        let report = run(&repo, true, false);
        assert_eq!(report.freed_bytes, 0);
        assert!(linked.join(".git").exists());
        assert!(report.skipped.iter().any(|s| s.contains("git repository")));
        let (_tmp, repo) = fixture(7200);
        let bare = repo.join("rust/target/mirror.git");
        git(&repo, &["init", "-q", "--bare", bare.to_str().unwrap()]);
        age_tree(&repo.join("rust/target"), 7200);
        let report = run(&repo, true, false);
        assert_eq!(report.freed_bytes, 0);
        assert!(bare.join("HEAD").exists());
        assert!(report.skipped.iter().any(|s| s.contains("git repository")));
    }

    #[test]
    fn a_pinned_target_is_refused_before_its_lease_file_is_opened() {
        use std::os::unix::fs::PermissionsExt;
        let (_tmp, repo) = fixture(7200);
        let target = repo.join("rust/target").canonicalize().unwrap();
        // A lease file this user cannot open for writing: opening it would fail the sweep.
        let lock = target.parent().unwrap().join("target.plangraph-lock");
        fs::write(&lock, b"").unwrap();
        fs::set_permissions(&lock, fs::Permissions::from_mode(0o400)).unwrap();
        let pinned = Guards {
            pinned: vec![target.clone()],
            live: vec![],
        };
        let mut report = Report::default();
        sweep_main_checkout_target(&repo, &pinned, SystemTime::now(), true, &mut report, || {
            Ok((vec![], false))
        })
        .unwrap();
        assert_eq!(report.freed_bytes, 0);
        assert!(report.skipped[0].contains("pinned or live"));
        assert!(target.join("debug/blob").exists());
    }

    #[test]
    fn a_held_cache_lease_keeps_an_idle_main_checkout_target() {
        let (_tmp, repo) = fixture(7200);
        let target = repo.join("rust/target").canonicalize().unwrap();
        let lease = crate::cargo_cache::try_exclusive_lease(&target)
            .unwrap()
            .unwrap();
        let report = run(&repo, true, false);
        assert_eq!(report.freed_bytes, 0);
        assert!(target.join("debug/blob").exists());
        assert!(report.skipped.iter().any(|s| s.contains("lease held")));
        drop(lease);
        assert!(run(&repo, true, false).freed_bytes > 0);
        assert!(!target.exists());
    }

    #[test]
    fn a_foreign_owned_target_is_kept() {
        let (_tmp, repo) = fixture(7200);
        let target = repo.join("rust/target");
        let mut report = Report::default();
        let foreign = |p: &Path| crate::private_targets::owned_by(p, u32::MAX - 1);
        sweep_owned(
            &repo,
            &Guards::default(),
            SystemTime::now(),
            true,
            &mut report,
            || Ok((vec![], false)),
            &foreign,
        )
        .unwrap();
        assert_eq!(report.freed_bytes, 0);
        assert!(target.join("debug/blob").exists());
        assert!(report.skipped[0].contains("owned by another user"));
    }

    #[test]
    fn recent_or_building_target_is_reported_but_kept() {
        let (_tmp, repo) = fixture(600);
        let report = run(&repo, true, false);
        assert!(repo.join("rust/target/debug/blob").exists());
        assert!(report.actions[0].starts_with("main_checkout_target"));
        assert_eq!(report.skipped.len(), 1);
        let (_tmp, repo) = fixture(7200);
        assert_eq!(run(&repo, true, true).freed_bytes, 0);
        assert!(repo.join("rust/target").exists());
    }

    #[test]
    fn a_directory_named_target_without_cargo_markers_is_never_removed() {
        let (_tmp, repo) = fixture(7200);
        fs::remove_file(repo.join("rust/target/CACHEDIR.TAG")).unwrap();
        let old = SystemTime::now() - Duration::from_secs(7200);
        for p in [repo.join("rust/target/debug"), repo.join("rust/target")] {
            fs::File::open(p).unwrap().set_modified(old).unwrap();
        }
        let report = run(&repo, true, false);
        assert_eq!(report.freed_bytes, 0);
        assert!(repo.join("rust/target/debug/blob").exists());
        assert!(report.skipped[0].contains("not an identified cargo target"));
        // A forged tag with the wrong signature is no evidence either.
        fs::write(repo.join("rust/target/CACHEDIR.TAG"), b"hello").unwrap();
        assert_eq!(run(&repo, true, false).freed_bytes, 0);
        // The rustc info file plus a profile directory is.
        fs::write(repo.join("rust/target/.rustc_info.json"), b"{}").unwrap();
        fs::remove_file(repo.join("rust/target/CACHEDIR.TAG")).unwrap();
        for p in [
            repo.join("rust/target/.rustc_info.json"),
            repo.join("rust/target"),
        ] {
            fs::File::open(p).unwrap().set_modified(old).unwrap();
        }
        assert!(run(&repo, true, false).freed_bytes > 0);
        assert!(!repo.join("rust/target").exists());
    }

    #[test]
    fn worktree_symlink_and_missing_targets_are_left_alone() {
        let (_tmp, repo) = fixture(7200);
        fs::remove_dir_all(repo.join(".git")).unwrap();
        fs::write(repo.join(".git"), "gitdir: elsewhere\n").unwrap();
        assert!(run(&repo, true, false).actions.is_empty());
        assert!(repo.join("rust/target").exists());
        let (_tmp, repo) = fixture(7200);
        let real = repo.join("real");
        fs::rename(repo.join("rust/target"), &real).unwrap();
        std::os::unix::fs::symlink(&real, repo.join("rust/target")).unwrap();
        assert!(run(&repo, true, false).actions.is_empty());
        assert!(real.join("debug/blob").exists());
        let (_tmp, repo) = fixture(7200);
        fs::remove_dir_all(repo.join("rust/target")).unwrap();
        assert!(run(&repo, true, false).actions.is_empty());
    }
}
