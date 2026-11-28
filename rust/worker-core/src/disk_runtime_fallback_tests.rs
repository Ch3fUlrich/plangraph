use super::hygiene::{place, resolve_hygiene, scan_hygiene_processes, scan_own_hygiene_processes};
use crate::{disk_hygiene::HygieneInventory, proc_mounts::Mount};
use std::{
    fs, io,
    os::unix::{
        ffi::OsStrExt,
        fs::{symlink, MetadataExt, PermissionsExt},
    },
    path::Path,
    time::{Duration, Instant},
};

/// A fixture proc entry: owner uid, process name, cwd, and whether its fd table is denied.
fn process(proc: &Path, pid: u32, uid: u32, comm: &str, cwd: &Path, denied: bool) {
    let dir = proc.join(pid.to_string());
    fs::create_dir_all(dir.join("fd")).unwrap();
    let stat = format!("{pid} ({comm}) S {} 789\n", "0 ".repeat(18));
    fs::write(dir.join("stat"), stat).unwrap();
    fs::write(
        dir.join("status"),
        format!("Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n"),
    )
    .unwrap();
    fs::write(dir.join("comm"), format!("{comm}\n")).unwrap();
    fs::write(dir.join("environ"), b"").unwrap();
    symlink(cwd, dir.join("cwd")).unwrap();
    if denied {
        fs::set_permissions(dir.join("fd"), fs::Permissions::from_mode(0o000)).unwrap();
    }
}

#[test]
fn reader_fault_falls_back_to_own_processes_and_names_the_fault() {
    // SAFETY: geteuid has no preconditions.
    let uid = unsafe { libc::geteuid() };
    let tmp = tempfile::tempdir().unwrap();
    let busy = tmp.path().join("tree-a");
    let idle = tmp.path().join("tree-b");
    let foreign_tree = tmp.path().join("tree-c");
    for tree in [&busy, &idle, &foreign_tree] {
        fs::create_dir(tree).unwrap();
    }
    let proc = tmp.path().join("proc");
    process(&proc, 100, uid, "cargo", &busy, false);
    // Another user's process with a denied fd table: the strict scan cannot see past it.
    process(&proc, 200, uid + 1, "rustc", &foreign_tree, uid != 0);
    // An own process that is not dumpable (denied) and no build tool: skipped.
    process(&proc, 300, uid, "gpg-agent", &idle, uid != 0);
    let deadline = Instant::now() + Duration::from_secs(10);
    let own = || scan_own_hygiene_processes(&proc, deadline, uid);
    let strict = scan_hygiene_processes(&proc, deadline);
    if uid != 0 {
        assert_eq!(strict.unwrap_err().kind(), io::ErrorKind::PermissionDenied);
    }
    let old_helper = || Err(io::Error::other("usage: pg-process-cwds [--references]"));
    let denied = || Err(io::Error::from(io::ErrorKind::PermissionDenied));
    let rows = resolve_hygiene(denied(), old_helper, own).unwrap();
    assert!(rows.blocks(&busy));
    assert!(
        !rows.blocks(&idle),
        "a denied own non-build process is skipped"
    );
    assert!(
        !rows.blocks(&foreign_tree),
        "other users' processes are not read"
    );
    let fault = rows.degraded.as_deref().unwrap();
    assert!(
        fault.starts_with("process reader incompatible: usage: pg-process-cwds"),
        "{fault}"
    );
    // A working reader stays authoritative and the inventory is not degraded.
    let reader = || Ok(HygieneInventory::default());
    let rows = resolve_hygiene(denied(), reader, || panic!("no fallback")).unwrap();
    assert!(rows.degraded.is_none() && !rows.blocks(&busy));
    // A readable direct scan needs neither.
    let direct = resolve_hygiene(own(), || panic!("no reader"), || panic!("no fallback"));
    assert!(direct.unwrap().blocks(&busy));
    if uid != 0 {
        // A denied own build process leaves no usable inventory.
        fs::write(proc.join("300/comm"), "cargo\n").unwrap();
        let error = resolve_hygiene(denied(), old_helper, own).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::PermissionDenied);
        for pid in ["200", "300"] {
            let fd = proc.join(pid).join("fd");
            fs::set_permissions(fd, fs::Permissions::from_mode(0o700)).unwrap();
        }
    }
}

#[test]
fn container_references_map_through_both_mount_tables() {
    let tmp = tempfile::tempdir().unwrap();
    let host = tmp.path().canonicalize().unwrap();
    let data = host.join("data");
    fs::create_dir(&data).unwrap();
    fs::write(data.join("db"), "x").unwrap();
    let meta = fs::metadata(data.join("db")).unwrap();
    let dev = meta.dev();
    let mount = |dev, root: &Path, point: &str| Mount {
        dev,
        root: root.to_path_buf(),
        point: point.into(),
    };
    // The container sees the host's data directory at /app/data; this namespace mounts the
    // device at its root.
    let theirs = vec![
        mount(1, Path::new("/"), "/"),
        mount(dev, &data, "/app/data"),
    ];
    let ours = vec![mount(dev, Path::new("/"), "/")];
    let foreign = (theirs, ours);
    let bytes = |path: &Path| path.as_os_str().as_bytes().to_vec();
    let placed = place(Path::new("/app/data/db"), Some(&meta), Some(&foreign)).unwrap();
    assert_eq!(placed, vec![bytes(&data.join("db"))]);
    // A deleted file maps to the host path it had.
    let deleted = place(
        Path::new("/app/data/gone (deleted)"),
        Some(&meta),
        Some(&foreign),
    );
    assert_eq!(deleted.unwrap(), vec![bytes(&data.join("gone"))]);
    // The container's own filesystem is not mounted here: nothing to protect, no failure.
    let elsewhere = (foreign.0.clone(), vec![mount(dev + 1, Path::new("/"), "/")]);
    assert!(
        place(Path::new("/app/data/db"), Some(&meta), Some(&elsewhere))
            .unwrap()
            .is_empty()
    );
    // The device is mounted here but no table maps the name: the inventory is unusable.
    let unmapped = (
        foreign.0.clone(),
        vec![mount(dev, Path::new("/srv"), "/srv")],
    );
    assert!(place(Path::new("/app/data/db"), Some(&meta), Some(&unmapped)).is_err());
    // The same name in this namespace names another object: a same-namespace reference fails
    // closed as before.
    assert!(place(&host.join("data"), Some(&meta), None).is_err());
}
