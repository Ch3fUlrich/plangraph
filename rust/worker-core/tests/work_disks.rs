use pg_worker_core::disk_runtime::DiskSpace;
use pg_worker_core::work_disks::{select_with, worktree_disk_with};
use std::{ffi::OsStr, io, path::Path};

#[test]
fn configured_selection_returns_the_largest_worktree_root_and_reports_every_refusal() {
    let mounts = std::env::join_paths(["/small", "/large"]).unwrap();
    let gib = 1 << 30;
    let sample = |path: &Path| {
        Ok(DiskSpace {
            free: if path == Path::new("/small") {
                10 * gib
            } else {
                20 * gib
            },
            total: 100 * gib,
        })
    };
    let (root, space) = worktree_disk_with(Some(&mounts), None, sample)
        .unwrap()
        .unwrap();
    assert_eq!(root, Path::new("/large/plangraph/worktrees"));
    assert_eq!(space.free, 20 * gib);
    let error = worktree_disk_with(Some(&mounts), Some(OsStr::new("21")), sample)
        .unwrap_err()
        .to_string();
    assert_eq!(error, "disk_low: no work mount meets 5 GiB and 21% free (/small: 10737418240 of 107374182400 bytes free; /large: 21474836480 of 107374182400 bytes free)");
}

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
