use pg_worker_core::disk_runtime::DiskSpace;
use pg_worker_core::work_disks::select_with;
use std::{io, path::Path};

#[test]
fn chooses_largest_eligible_mount_and_names_failures() {
    const GIB: u64 = 1 << 30;
    let mounts = [
        Path::new("/build-a"),
        Path::new("/build-b"),
        Path::new("/build-c"),
    ];
    let sample = |path: &Path| match path.to_str().unwrap() {
        "/build-a" => Ok(DiskSpace {
            free: 45 * GIB,
            total: 500 * GIB,
        }),
        "/build-b" => Ok(DiskSpace {
            free: 30 * GIB,
            total: 100 * GIB,
        }),
        _ => Err(io::Error::other("mount unavailable")),
    };
    assert_eq!(select_with(&mounts, 10, sample).unwrap().0, mounts[1]);
    let error = select_with(&[mounts[0], mounts[2]], 10, sample)
        .unwrap_err()
        .to_string();
    assert!(error.starts_with("disk_low:"), "{error}");
    assert!(
        error.contains("/build-a") && error.contains("/build-c"),
        "{error}"
    );
}
