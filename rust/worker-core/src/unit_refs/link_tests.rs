//! Unit links, linked drop-in directories and worktree-root checks.
use super::*;

fn protected(refs: &[UnitRef], path: &str) -> bool {
    refs.iter().any(|r| r.path == Path::new(path))
}
fn exact(refs: &[UnitRef], path: &str) -> bool {
    refs.iter().any(|r| r.exact && r.path == Path::new(path))
}

#[cfg(unix)]
#[test]
fn symlinked_drop_in_directories_and_relative_link_targets_are_followed() {
    use std::os::unix::fs::symlink;
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("systemd/user");
    let real = tmp.path().join("shared/app.service.d");
    fs::create_dir_all(&dir).unwrap();
    fs::create_dir_all(&real).unwrap();
    fs::write(
        real.join("o.conf"),
        "[Service]\nExecStart=/wt/app-worktrees/d1/run\n",
    )
    .unwrap();
    symlink(&real, dir.join("app.service.d")).unwrap();
    // A relative link target resolves against the link's directory.
    fs::create_dir_all(tmp.path().join("systemd/app-worktrees/d2")).unwrap();
    symlink(
        "../app-worktrees/d2/unit.service",
        dir.join("linked.service"),
    )
    .unwrap();
    // A directory link back to its parent must not loop forever.
    symlink(&dir, dir.join("loop.d")).unwrap();
    let refs = unit_file_refs(&dir, Path::new("/h")).unwrap();
    assert!(
        refs.iter()
            .any(|r| r.unit == "app.service" && r.path == Path::new("/wt/app-worktrees/d1/run")),
        "{refs:?}"
    );
    let linked = tmp
        .path()
        .join("systemd/app-worktrees/d2/unit.service")
        .canonicalize()
        .unwrap_or_else(|_| tmp.path().join("systemd/app-worktrees/d2/unit.service"));
    assert!(
        refs.iter()
            .any(|r| r.unit == "linked.service" && r.path == linked),
        "{refs:?}"
    );
}

#[cfg(unix)]
#[test]
fn a_dangling_link_target_with_spaces_only_protects() {
    use std::os::unix::fs::symlink;
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("systemd/user");
    fs::create_dir_all(&dir).unwrap();
    symlink(
        "/wt/app-worktrees/gone blue/unit.service",
        dir.join("a.service"),
    )
    .unwrap();
    symlink(
        "/wt/app-worktrees/gone2/unit.service",
        dir.join("b.service"),
    )
    .unwrap();
    let refs = unit_file_refs(&dir, Path::new("/h")).unwrap();
    assert!(
        protected(&refs, "/wt/app-worktrees/gone blue/unit.service"),
        "{refs:?}"
    );
    assert!(
        !exact(&refs, "/wt/app-worktrees/gone blue/unit.service"),
        "{refs:?}"
    );
    assert!(
        exact(&refs, "/wt/app-worktrees/gone2/unit.service"),
        "{refs:?}"
    );
}

#[cfg(unix)]
#[test]
fn a_link_target_keeps_its_named_path_even_when_it_is_itself_a_link() {
    use std::os::unix::fs::symlink;
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("systemd/user");
    let wt = tmp.path().join("app-worktrees/wt1");
    fs::create_dir_all(&dir).unwrap();
    fs::create_dir_all(&wt).unwrap();
    fs::create_dir_all(tmp.path().join("elsewhere")).unwrap();
    fs::write(tmp.path().join("elsewhere/real.service"), "[Service]\n").unwrap();
    symlink(
        tmp.path().join("elsewhere/real.service"),
        wt.join("unit.service"),
    )
    .unwrap();
    symlink(wt.join("unit.service"), dir.join("x.service")).unwrap();
    let refs = unit_file_refs(&dir, Path::new("/h")).unwrap();
    assert!(
        refs.iter()
            .any(|r| r.exact && r.path == wt.join("unit.service")),
        "{refs:?}"
    );
    let canonical = wt.canonicalize().unwrap();
    assert_eq!(
        unit_names(&refs, &wt, &canonical),
        ["x.service"],
        "{refs:?}"
    );
}

#[cfg(unix)]
#[test]
fn a_relative_link_in_a_linked_drop_in_directory_resolves_from_its_physical_parent() {
    use std::os::unix::fs::symlink;
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("systemd/user");
    let real = tmp.path().join("shared/real.d");
    fs::create_dir_all(&dir).unwrap();
    fs::create_dir_all(&real).unwrap();
    symlink("../app-worktrees/d3/u.service", real.join("link.service")).unwrap();
    symlink(&real, dir.join("app.service.d")).unwrap();
    let refs = unit_file_refs(&dir, Path::new("/h")).unwrap();
    let expected = tmp.path().join("shared/app-worktrees/d3/u.service");
    assert!(
        refs.iter().any(|r| r.exact && r.path == expected),
        "{refs:?}"
    );
    assert!(
        !refs
            .iter()
            .any(|r| r.path == tmp.path().join("systemd/app-worktrees/d3/u.service")),
        "{refs:?}"
    );
}

#[cfg(unix)]
#[test]
fn two_units_sharing_a_linked_drop_in_directory_are_both_attributed() {
    use std::os::unix::fs::symlink;
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("systemd/user");
    let common = tmp.path().join("shared/common.d");
    fs::create_dir_all(&dir).unwrap();
    fs::create_dir_all(&common).unwrap();
    fs::write(
        common.join("o.conf"),
        "[Service]\nExecStart=/wt/app-worktrees/shared1/run\n",
    )
    .unwrap();
    symlink(&common, dir.join("a.service.d")).unwrap();
    symlink(&common, dir.join("b.service.d")).unwrap();
    let refs = unit_file_refs(&dir, Path::new("/h")).unwrap();
    let tree = Path::new("/wt/app-worktrees/shared1");
    assert_eq!(
        unit_names(&refs, tree, tree),
        ["a.service", "b.service"],
        "{refs:?}"
    );
}

#[cfg(unix)]
#[test]
fn dot_dot_after_a_linked_directory_resolves_physically() {
    use std::os::unix::fs::symlink;
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("systemd/user");
    let deep = tmp.path().join("other/deep");
    let live = tmp.path().join("other/wt-worktrees/live");
    fs::create_dir_all(&dir).unwrap();
    fs::create_dir_all(&deep).unwrap();
    fs::create_dir_all(&live).unwrap();
    symlink(&deep, dir.join("alias")).unwrap();
    // `alias/..` is `other` (the link's target parent), not `systemd/user`'s parent.
    symlink(
        "alias/../wt-worktrees/live/unit.service",
        dir.join("x.service"),
    )
    .unwrap();
    let refs = unit_file_refs(&dir, Path::new("/h")).unwrap();
    let canonical = live.canonicalize().unwrap();
    assert_eq!(
        unit_names(&refs, &live, &canonical),
        ["x.service"],
        "{refs:?}"
    );
    assert!(
        refs.iter()
            .any(|r| r.exact && r.path == canonical.join("unit.service")),
        "{refs:?}"
    );
}

#[cfg(unix)]
#[test]
fn a_link_target_ending_in_dot_dot_or_crossing_several_links_resolves_physically() {
    use std::os::unix::fs::symlink;
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("systemd/user");
    let live = tmp.path().join("trees/app-worktrees/live");
    let other = tmp.path().join("trees/app-worktrees/other");
    fs::create_dir_all(&dir).unwrap();
    fs::create_dir_all(live.join("deep")).unwrap();
    fs::create_dir_all(other.join("inner")).unwrap();
    symlink(live.join("deep"), dir.join("alias")).unwrap();
    symlink(other.join("inner"), dir.join("alias2")).unwrap();
    // Ends in `..`: the link's target is the worktree `live` itself.
    symlink("alias/..", dir.join("end.service")).unwrap();
    // Two links in a row, then `../..`: `alias/../../app-worktrees/other` is trees/app-worktrees/other.
    symlink("alias2/../../other/run.sh", dir.join("two.service")).unwrap();
    let refs = unit_file_refs(&dir, Path::new("/h")).unwrap();
    let live_c = live.canonicalize().unwrap();
    let other_c = other.canonicalize().unwrap();
    // (The `alias` directory links name live/deep and other/inner, so they protect those trees too.)
    let names = |tree: &Path, canonical: &Path| unit_names(&refs, tree, canonical);
    assert!(
        names(&live, &live_c).contains(&"end.service".to_string()),
        "{refs:?}"
    );
    assert!(
        names(&other, &other_c).contains(&"two.service".to_string()),
        "{refs:?}"
    );
    assert!(refs.iter().any(|r| r.exact && r.path == live_c), "{refs:?}");
}
