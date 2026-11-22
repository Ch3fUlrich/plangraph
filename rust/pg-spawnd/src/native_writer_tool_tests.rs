use super::*;

const MOUNTS: &str = r"1 0 8:1 / / rw - ext4 /dev/root rw
2 1 8:2 / /mnt/data rw - ext4 /dev/data rw
3 1 0:3 / /mnt/c rw - 9p C:\ rw
4 3 8:4 / /mnt/c/linux rw - ext4 /dev/data rw
5 1 0:5 / /mnt/windows\040share rw - drvfs D:\ rw";

#[test]
fn refuses_windows_mount_codex_before_launch() {
    for (path, mountinfo) in [
        ("/mnt/c/bin/codex", MOUNTS),
        ("/mnt/windows share/bin/codex", MOUNTS),
        ("/mnt/d/bin/codex", ""),
    ] {
        let error = require_native(Path::new(path), true, mountinfo).unwrap_err();
        assert!(error.starts_with("tool_wrong_platform:"), "{error}");
        assert!(error.contains(path), "{error}");
    }
}

#[cfg(unix)]
fn executable(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, "#!/bin/sh\nprintf linux-codex").unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
}

#[cfg(unix)]
#[test]
fn linux_path_export_selects_and_runs_linux_codex() {
    let root = tempfile::tempdir().unwrap();
    let linux_bin = root.path().join(".local/bin");
    let inherited_bin = root.path().join("windows-npm");
    executable(&linux_bin.join("codex"));
    executable(&inherited_bin.join("codex"));
    let path = std::env::join_paths([&linux_bin, &inherited_bin]).unwrap();
    let selected = resolve(&path, root.path()).unwrap();
    assert_eq!(selected, linux_bin.join("codex"));
    let result = std::process::Command::new(selected).output().unwrap();
    assert!(result.status.success());
    assert_eq!(result.stdout, b"linux-codex");
}

#[cfg(unix)]
#[test]
fn skips_non_executables_and_resolves_relative_path_from_writer_cwd() {
    let root = tempfile::tempdir().unwrap();
    let disabled = root.path().join("disabled");
    fs::create_dir(&disabled).unwrap();
    fs::write(disabled.join("codex"), "not executable").unwrap();
    executable(&root.path().join("bin/codex"));
    let path = std::env::join_paths([disabled.as_path(), Path::new("bin")]).unwrap();
    assert_eq!(
        resolve(&path, root.path()).unwrap(),
        root.path().join("bin/codex")
    );
}

#[cfg(target_os = "linux")]
#[test]
fn refuses_linux_symlink_into_windows_mount() {
    let root = tempfile::tempdir().unwrap();
    let link = root.path().join("codex");
    let target = root.path().join("windows-drive").join("codex");
    executable(&target);
    let canonical = target.canonicalize().unwrap();
    std::os::unix::fs::symlink(&target, &link).unwrap();
    let error = checked_path(link, |path| {
        if path == canonical {
            require_native(Path::new("/mnt/c/bin/codex"), true, MOUNTS)
        } else {
            Ok(())
        }
    })
    .unwrap_err();
    assert!(error.starts_with("tool_wrong_platform:"), "{error}");
}

#[test]
fn missing_codex_reports_tool_missing() {
    let root = tempfile::tempdir().unwrap();
    let error = resolve(root.path().as_os_str(), root.path()).unwrap_err();
    assert!(error.starts_with("tool_missing:"), "{error}");
}

#[cfg(unix)]
#[test]
fn linux_npm_symlink_keeps_selected_path() {
    let root = tempfile::tempdir().unwrap();
    let target = root.path().join("lib/codex");
    executable(&target);
    let link = root.path().join("codex");
    std::os::unix::fs::symlink(&target, &link).unwrap();
    assert_eq!(resolve(root.path().as_os_str(), root.path()).unwrap(), link);
}

#[test]
fn accepts_a_linux_data_disk_below_mnt() {
    for (path, is_wsl) in [
        ("/mnt/data/bin/codex", false),
        ("/mnt/data/bin/codex", true),
        ("/mnt/c/bin/codex", false),
        ("/mnt/c/linux/bin/codex", true),
    ] {
        require_native(Path::new(path), is_wsl, MOUNTS).unwrap();
    }
}
