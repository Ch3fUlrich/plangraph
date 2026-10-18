//! Directory confinement: verify a caller-supplied work directory is the
//! card work root itself or inside it, after resolving symlinks.

use std::io;
use std::path::{Component, Path, PathBuf};

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

/// Resolve `candidate` and verify it is `root` itself or inside `root`.
/// Returns the canonical path to run in. Errors are `io::ErrorKind::InvalidInput`
/// (a path escape / invalid input; never retryable) with a message that does
/// NOT echo the full paths.
///
/// Explicit rule: any `..` (parent-dir) component anywhere in the candidate
/// is refused outright, even if the resulting path would stay inside the
/// root — the rule is deliberately simple so no traversal trickery can slip
/// past it.
pub fn confine_dir(root: &Path, candidate: &str) -> io::Result<PathBuf> {
    if !root.is_dir() {
        return Err(invalid("workroot is not a directory"));
    }
    let canonical_root = root
        .canonicalize()
        .map_err(|_| invalid("workroot is not a directory"))?;

    if candidate.trim().is_empty() || candidate.contains('\0') {
        return Err(invalid("path is empty"));
    }
    let candidate_path = Path::new(candidate);
    for comp in candidate_path.components() {
        if matches!(comp, Component::ParentDir) {
            return Err(invalid("path contains a parent-dir component"));
        }
    }

    let joined = if candidate_path.is_absolute() {
        candidate_path.to_path_buf()
    } else {
        canonical_root.join(candidate_path)
    };

    // Walk down to the longest existing prefix (a dangling symlink counts as
    // existing), canonicalize it, then re-append the non-existent tail. The
    // tail cannot contain `..` thanks to the parent-dir rule above.
    let mut ancestors: Vec<&Path> = Vec::new();
    let mut p: &Path = &joined;
    loop {
        match std::fs::symlink_metadata(p) {
            Ok(_) => break,
            Err(e) if e.kind() == io::ErrorKind::NotFound => {
                ancestors.push(p);
                match p.parent() {
                    Some(parent) => p = parent,
                    None => {
                        p = &canonical_root;
                        break;
                    }
                }
            }
            Err(_) => {
                p = &canonical_root;
                break;
            }
        }
    }

    let mut result = p
        .canonicalize()
        .map_err(|_| invalid("gate workdir is not a directory"))?;
    for missing in ancestors.iter().rev() {
        let Ok(tail) = missing.strip_prefix(p) else {
            return Err(invalid("path escapes the work root"));
        };
        for comp in tail.components() {
            if let Component::Normal(name) = comp {
                result.push(name);
            }
        }
    }

    // Prefix check must precede the directory check, so an escape is
    // reported as an escape even when the target also is not a directory.
    // Path::starts_with is component-wise, so /root-evil does not match /root.
    if !result.starts_with(&canonical_root) {
        return Err(invalid("path escapes the work root"));
    }
    if !result.is_dir() {
        return Err(invalid("gate workdir is not a directory"));
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn expect_err(candidate: &str, root: &Path, msg: &str) {
        let err = confine_dir(root, candidate).expect_err("must be refused");
        assert_eq!(err.kind(), io::ErrorKind::InvalidInput);
        assert_eq!(
            err.to_string(),
            msg,
            "candidate {candidate:?} refused for the wrong reason"
        );
    }

    #[test]
    fn root_itself_relative_and_absolute() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("work");
        std::fs::create_dir(&root).unwrap();
        assert_eq!(
            confine_dir(&root, ".").unwrap(),
            root.canonicalize().unwrap()
        );
    }

    #[cfg(unix)]
    #[test]
    fn root_itself_via_absolute_path() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("work");
        std::fs::create_dir(&root).unwrap();
        let abs = absolute(&root.canonicalize().unwrap());
        assert_eq!(
            confine_dir(&root, &abs).unwrap(),
            root.canonicalize().unwrap()
        );
    }

    #[test]
    fn existing_subdir_relative() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("work");
        std::fs::create_dir(&root).unwrap();
        let sub = root.join("sub");
        std::fs::create_dir(&sub).unwrap();
        assert_eq!(
            confine_dir(&root, "sub").unwrap(),
            sub.canonicalize().unwrap()
        );
    }

    #[cfg(unix)]
    #[test]
    fn existing_subdir_absolute() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("work");
        std::fs::create_dir(&root).unwrap();
        let sub = root.join("sub");
        std::fs::create_dir(&sub).unwrap();
        let abs = absolute(&sub.canonicalize().unwrap());
        assert_eq!(
            confine_dir(&root, &abs).unwrap(),
            sub.canonicalize().unwrap()
        );
    }

    #[test]
    fn file_is_refused_as_not_a_directory() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("work");
        std::fs::create_dir(&root).unwrap();
        std::fs::write(root.join("F"), b"x").unwrap();
        expect_err("F", &root, "gate workdir is not a directory");
    }

    #[test]
    fn empty_and_whitespace_and_nul() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("work");
        std::fs::create_dir(&root).unwrap();
        expect_err("", &root, "path is empty");
        expect_err("   ", &root, "path is empty");
        expect_err("a\0b", &root, "path is empty");
    }

    #[test]
    fn non_existent_root_is_refused() {
        let tmp = tempfile::tempdir().unwrap();
        let missing = tmp.path().join("no-such-root");
        let err = confine_dir(&missing, ".").expect_err("must be refused");
        assert_eq!(err.kind(), io::ErrorKind::InvalidInput);
        assert_eq!(err.to_string(), "workroot is not a directory");
    }

    #[cfg(unix)]
    fn absolute(p: &Path) -> String {
        p.as_os_str().to_str().expect("utf-8 path").to_string()
    }

    #[cfg(unix)]
    fn make_symlink(target: &Path, link: &Path) {
        std::os::unix::fs::symlink(target, link).unwrap();
    }

    #[cfg(all(test, unix))]
    #[test]
    fn nested_relative_subdirs() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("work");
        std::fs::create_dir(&root).unwrap();
        std::fs::create_dir_all(root.join("a/b")).unwrap();
        assert_eq!(
            confine_dir(&root, "a/b").unwrap(),
            root.join("a/b").canonicalize().unwrap()
        );
    }

    #[cfg(all(test, unix))]
    #[test]
    fn parent_dir_components_are_refused() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("work");
        std::fs::create_dir(&root).unwrap();
        std::fs::create_dir(root.join("sub")).unwrap();
        expect_err("../x", &root, "path contains a parent-dir component");
        expect_err("sub/../../x", &root, "path contains a parent-dir component");
        expect_err("..", &root, "path contains a parent-dir component");
        expect_err("sub/..", &root, "path contains a parent-dir component");
    }

    #[cfg(all(test, unix))]
    #[test]
    fn outside_paths_are_refused() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("work");
        std::fs::create_dir(&root).unwrap();
        expect_err(absolute(tmp.path()), &root, "path escapes the work root");
        expect_err("/", &root, "path escapes the work root");
    }

    #[cfg(all(test, unix))]
    #[test]
    fn sibling_prefix_attack_is_refused() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("work");
        std::fs::create_dir(&root).unwrap();
        let evil = tmp.path().join("work-evil");
        std::fs::create_dir(&evil).unwrap();
        expect_err(absolute(&evil), &root, "path escapes the work root");
    }

    #[cfg(all(test, unix))]
    #[test]
    fn symlink_to_outside_is_refused() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("work");
        std::fs::create_dir(&root).unwrap();
        let outside = tmp.path().join("outside");
        std::fs::create_dir(&outside).unwrap();
        make_symlink(&outside, &root.join("link"));
        expect_err("link", &root, "path escapes the work root");
        std::fs::create_dir(outside.join("inner")).unwrap();
        expect_err("link/inner", &root, "path escapes the work root");
    }

    #[cfg(all(test, unix))]
    #[test]
    fn symlink_to_inside_is_allowed() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("work");
        std::fs::create_dir(&root).unwrap();
        let real = root.join("real");
        std::fs::create_dir(&real).unwrap();
        make_symlink(&real, &root.join("link"));
        assert_eq!(
            confine_dir(&root, "link").unwrap(),
            real.canonicalize().unwrap()
        );
    }

    #[cfg(all(test, unix))]
    #[test]
    fn symlink_root_resolves_to_canonical_target() {
        let tmp = tempfile::tempdir().unwrap();
        let work = tmp.path().join("work");
        std::fs::create_dir(&work).unwrap();
        let rootlink = tmp.path().join("rootlink");
        make_symlink(&work, &rootlink);
        std::fs::create_dir(work.join("sub")).unwrap();
        assert_eq!(
            confine_dir(&rootlink, "sub").unwrap(),
            work.join("sub").canonicalize().unwrap()
        );
    }

    #[cfg(all(test, unix))]
    #[test]
    fn dangling_symlink_is_refused() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("work");
        std::fs::create_dir(&root).unwrap();
        make_symlink(&root.join("no-target"), &root.join("dangling"));
        expect_err("dangling", &root, "gate workdir is not a directory");
    }

    #[cfg(all(test, unix))]
    #[test]
    fn non_existent_in_root_path_is_refused_as_not_a_directory() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("work");
        std::fs::create_dir(&root).unwrap();
        expect_err("nope/deeper", &root, "gate workdir is not a directory");
    }

    #[cfg(unix)]
    #[test]
    fn error_messages_never_echo_the_paths() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("work");
        std::fs::create_dir(&root).unwrap();
        std::fs::create_dir(root.join("sub")).unwrap();
        let err = confine_dir(&root, "sub/..").unwrap_err();
        assert!(!err.to_string().contains(&root.display().to_string()));
    }
}
