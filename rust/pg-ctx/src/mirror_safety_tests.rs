use super::*;
use std::{cell::Cell, time::Duration};

#[test]
fn mirror_lock_holder_child() {
    use std::io::Write;
    let Some(path) = std::env::var_os("PG_CTX_LOCK_FIXTURE") else {
        // A direct run still exercises acquisition and release on its own file.
        let tmp = tempfile::tempdir().unwrap();
        assert!(lockfile::try_lock(&tmp.path().join("lock"))
            .unwrap()
            .is_some());
        return;
    };
    let _guard = lockfile::try_lock(Path::new(&path)).unwrap().unwrap();
    println!("locked");
    std::io::stdout().flush().unwrap();
    let mut release = String::new();
    std::io::stdin().read_line(&mut release).unwrap();
    assert_eq!(release, "release\n");
}

#[test]
fn mirror_fetch_lock_deadline_reports_fetch_failed() {
    let elapsed = Cell::new(Duration::ZERO);
    let attempts = Cell::new(0);
    let result = fetch_lock_wait::<()>(
        || {
            attempts.set(attempts.get() + 1);
            assert!(attempts.get() < 1000, "fetch.lock wait was not bounded");
            Ok(None)
        },
        || elapsed.get(),
        |delay| elapsed.set(elapsed.get() + delay),
    );
    assert_eq!(result, Err(Alarm::FetchFailed));
    assert_eq!(elapsed.get(), FETCH_LOCK_TIMEOUT);
}

#[test]
fn mirror_publish_interruption_keeps_accepted_repository() {
    let f = Fixture::new();
    let a = f.commit(None);
    f.mirror.fetch("demo").unwrap();
    let bytes = f.bytes();
    let accepted = f.mirror.mirror_dir("demo").unwrap();
    let b = f.commit(Some(&a));
    let mut visible = false;
    {
        let staging = tempfile::tempdir_in(accepted.parent().unwrap()).unwrap();
        let candidate = staging.path().join("mirror.git");
        copy_repository(&f.origin, &candidate).unwrap();
        assert!(publish_with(&candidate, &accepted, || {
            visible = accepted.exists();
            Err(io::Error::other(
                "simulated interruption after first rename",
            ))
        })
        .is_err());
    }
    assert!(
        visible,
        "lockless readers must always have an accepted mirror"
    );
    assert!(
        accepted.exists(),
        "TempDir drop must not destroy accepted history"
    );
    let main = git(&accepted, &["rev-parse", "main"]);
    assert!(main == a || main == b);
    assert_eq!(f.bytes(), bytes);
    assert!(git_command(&accepted)
        .args(["cat-file", "-e", &a])
        .status()
        .unwrap()
        .success());
}

#[test]
fn mirror_publish_unwind_and_failed_exchange_keep_accepted_repository() {
    let f = Fixture::new();
    let a = f.commit(None);
    f.mirror.fetch("demo").unwrap();
    let accepted = f.mirror.mirror_dir("demo").unwrap();
    let parent = accepted.parent().unwrap();
    let bytes = f.bytes();
    assert!(publish_with(&parent.join("missing.git"), &accepted, || Ok(())).is_err());
    assert_eq!(git(&accepted, &["rev-parse", "main"]), a);
    let b = f.commit(Some(&a));
    let staging = tempfile::Builder::new()
        .prefix(STAGING_PREFIX)
        .tempdir_in(parent)
        .unwrap();
    let candidate = staging.path().join("mirror.git");
    copy_repository(&f.origin, &candidate).unwrap();
    let interrupted = std::panic::catch_unwind(|| {
        let _staging = staging;
        let _ = publish_with(&candidate, &accepted, || panic!("simulated unwind"));
    });
    assert!(interrupted.is_err());
    assert_eq!(git(&accepted, &["rev-parse", "main"]), b);
    assert_eq!(f.bytes(), bytes);
    // A process abort leaves only a named quarantine, swept on the next fetch.
    let orphan = tempfile::Builder::new()
        .prefix(STAGING_PREFIX)
        .tempdir_in(parent)
        .unwrap()
        .keep();
    fs::write(orphan.join("abandoned"), "fixture").unwrap();
    assert_eq!(f.mirror.fetch("demo").unwrap().main_head, b);
    assert!(!orphan.exists());
}

#[test]
fn mirror_interrupted_publication_does_not_allow_rewind_to_old_watermark() {
    let f = Fixture::new();
    let a = f.commit(None);
    f.mirror.fetch("demo").unwrap();
    let accepted = f.mirror.mirror_dir("demo").unwrap();
    let bytes = f.bytes();
    let b = f.commit(Some(&a));
    let staging = tempfile::tempdir_in(accepted.parent().unwrap()).unwrap();
    let candidate = staging.path().join("mirror.git");
    copy_repository(&f.origin, &candidate).unwrap();
    assert!(publish_with(&candidate, &accepted, || Err(io::Error::other(
        "watermark not yet written"
    )))
    .is_err());
    git(&f.origin, &["update-ref", "refs/heads/main", &a]);
    assert_eq!(f.mirror.fetch("demo"), Err(Alarm::ForcePush));
    assert_eq!(git(&accepted, &["rev-parse", "main"]), b);
    assert_eq!(f.bytes(), bytes);
}

#[cfg(unix)]
#[test]
fn mirror_repository_copy_refuses_symlinks_and_keeps_mutable_files_private() {
    use std::os::unix::fs::{symlink, MetadataExt};
    let f = Fixture::new();
    f.commit(None);
    f.mirror.fetch("demo").unwrap();
    let accepted = f.mirror.mirror_dir("demo").unwrap();
    let staging = tempfile::tempdir().unwrap();
    let candidate = staging.path().join("candidate.git");
    copy_repository(&accepted, &candidate).unwrap();
    for file in ["config", "refs/heads/main"] {
        assert_ne!(
            fs::metadata(accepted.join(file)).unwrap().ino(),
            fs::metadata(candidate.join(file)).unwrap().ino()
        );
        fs::write(candidate.join(file), "changed").unwrap();
        assert_ne!(fs::read(accepted.join(file)).unwrap(), b"changed");
    }
    symlink(f._tmp.path().join("outside"), accepted.join("linked")).unwrap();
    assert_eq!(f.mirror.fetch("demo"), Err(Alarm::FetchFailed));
    assert!(copy_repository(&accepted, &staging.path().join("refused.git")).is_err());
}

#[test]
fn mirror_gix_local_file_url_fetches_packed_history_and_prunes_refs() {
    let f = Fixture::new();
    let a = f.commit(None);
    git(&f.origin, &["update-ref", "refs/heads/other", &a]);
    git(&f.origin, &["update-ref", "refs/tags/fixture", &a]);
    git(&f.origin, &["gc", "--prune=now"]);
    let config = f._tmp.path().join("file-url.json");
    fs::write(
        &config,
        serde_json::to_vec(&serde_json::json!({
            "demo": format!("file://{}", f.origin.display())
        }))
        .unwrap(),
    )
    .unwrap();
    let mirror = Mirror::from_repos(&config, &f.mirror.root).unwrap();
    assert_eq!(mirror.fetch("demo").unwrap().main_head, a);
    let b = f.commit(Some(&a));
    assert_eq!(mirror.fetch("demo").unwrap().main_head, b);
    let accepted = mirror.mirror_dir("demo").unwrap();
    assert_eq!(
        git(&accepted, &["for-each-ref", "--format=%(refname)"]),
        "refs/heads/main"
    );
    git(&f.origin, &["update-ref", "-d", "refs/heads/main"]);
    let bytes = f.bytes();
    assert_eq!(mirror.fetch("demo"), Err(Alarm::FetchFailed));
    assert_eq!(f.bytes(), bytes);
}

#[test]
fn mirror_gix_refuses_helper_transports_before_connecting() {
    let f = Fixture::new();
    f.commit(None);
    f.mirror.fetch("demo").unwrap();
    let bytes = f.bytes();
    let repository = open(&f.mirror.mirror_dir("demo").unwrap()).unwrap();
    for url in ["ssh://example.invalid/repository.git", "ext::git helper"] {
        assert_eq!(backend::fetch(&repository, url), Err(Alarm::FetchFailed));
    }
    assert_eq!(f.bytes(), bytes);
}

#[cfg(unix)]
#[test]
fn mirror_fetch_reuses_unchanged_object_files() {
    use std::os::unix::fs::MetadataExt;
    fn objects(dir: &Path) -> Vec<(PathBuf, u64, u64)> {
        let mut out = Vec::new();
        for entry in fs::read_dir(dir).unwrap() {
            let entry = entry.unwrap();
            if entry.file_type().unwrap().is_dir() {
                // objects/info contains mutable metadata, not object payloads.
                if entry.file_name() != "info" {
                    out.extend(objects(&entry.path()));
                }
            } else {
                let metadata = entry.metadata().unwrap();
                out.push((entry.path(), metadata.dev(), metadata.ino()));
            }
        }
        out
    }
    let f = Fixture::new();
    let a = f.commit(None);
    f.mirror.fetch("demo").unwrap();
    let accepted = f.mirror.mirror_dir("demo").unwrap();
    // Exercise both loose objects and immutable pack/index files.
    git(&accepted, &["repack", "-ad"]);
    let b = f.commit(Some(&a));
    f.mirror.fetch("demo").unwrap();
    let before = objects(&accepted.join("objects"));
    assert!(!before.is_empty());
    assert!(before
        .iter()
        .any(|(path, _, _)| path.extension().is_some_and(|ext| ext == "pack")));
    assert_eq!(f.mirror.fetch("demo").unwrap().main_head, b);
    for (path, device, inode) in before {
        let metadata = fs::metadata(path).unwrap();
        assert_eq!(
            (metadata.dev(), metadata.ino()),
            (device, inode),
            "unchanged object copied again"
        );
    }
}
