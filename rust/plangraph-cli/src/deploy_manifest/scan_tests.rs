use super::tests::{bins, set};
use super::*;

/// A temporary repository with `tracked` committed to the index and `untracked` only on disk.
pub(super) fn repo_with(tracked: &[(&str, &str)], untracked: &[(&str, &str)]) -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    let write = |files: &[(&str, &str)]| {
        for (path, text) in files {
            let full = dir.path().join(path);
            std::fs::create_dir_all(full.parent().unwrap()).unwrap();
            std::fs::write(full, text).unwrap();
        }
    };
    let git = |args: &[&str]| {
        let status = std::process::Command::new("git")
            .arg("-C")
            .arg(dir.path())
            .args(args)
            .env_remove("GIT_DIR")
            .env_remove("GIT_INDEX_FILE")
            .status()
            .unwrap();
        assert!(status.success(), "git {args:?}");
    };
    git(&["init", "-q"]);
    write(tracked);
    git(&["add", "-A"]);
    write(untracked);
    dir
}

#[test]
fn unit_files_outside_deploy_systemd_need_a_placement_or_an_exclusion() {
    let dir = repo_with(
        &[
            ("deploy/systemd/a.service", "[Unit]\n"),
            ("deploy/liveness/w@.service", "[Unit]\n"),
            ("deploy/pganchor/p.socket", "[Unit]\n"),
            ("deploy/liveness/router.toml", "x = 1\n"),
        ],
        &[],
    );
    let shipped = super::scan::unit_files(dir.path()).unwrap();
    assert_eq!(
        shipped.iter().map(String::as_str).collect::<Vec<_>>(),
        [
            "liveness/w@.service",
            "pganchor/p.socket",
            "systemd/a.service"
        ]
    );
    let only_systemd =
        Manifest::parse("[[unit]]\nname='a'\nfiles=['a.service']\nhosts=['all']\nfirst_run='x'\n")
            .unwrap();
    let problems = only_systemd.coverage(&shipped, &Default::default());
    assert_eq!(problems.len(), 2, "{problems:?}");
    assert!(problems
        .iter()
        .any(|p| p.contains("deploy/liveness/w@.service")));
    assert!(problems
        .iter()
        .any(|p| p.contains("deploy/pganchor/p.socket")));
    let covered = Manifest::parse(
        "[[unit]]\nname='a'\nfiles=['a.service']\nhosts=['all']\nfirst_run='x'\n\
         [[unit]]\nname='w'\ndir='liveness'\nfiles=['w@.service']\nhosts=['all']\nfirst_run='x'\n\
         [[unit]]\nname='p'\ndir='pganchor'\nfiles=['p.socket']\nscope='system'\nhosts=['keeper']\nfirst_run='x'\n",
    )
    .unwrap();
    assert!(covered.coverage(&shipped, &Default::default()).is_empty());
    let excluded = Manifest::parse(
        "[[unit]]\nname='a'\nfiles=['a.service']\nhosts=['all']\nfirst_run='x'\n\
         [[unit]]\nname='w'\ndir='liveness'\nfiles=['w@.service']\nnot_deployed='needs a collector'\n\
         [[unit]]\nname='p'\ndir='pganchor'\nfiles=['p.socket']\nnot_deployed='root only'\n",
    )
    .unwrap();
    assert!(excluded.coverage(&shipped, &Default::default()).is_empty());
}

#[test]
fn an_unlisted_unit_of_any_systemd_type_fails_coverage() {
    for ext in [
        "service",
        "timer",
        "socket",
        "target",
        "mount",
        "path",
        "automount",
        "swap",
        "slice",
        "scope",
        "device",
    ] {
        let dir = repo_with(
            &[
                (&format!("deploy/systemd/x.{ext}"), "[Unit]\n"),
                ("deploy/systemd/x.example", "not a unit\n"),
            ],
            &[],
        );
        let shipped = super::scan::unit_files(dir.path()).unwrap();
        assert_eq!(shipped.len(), 1, "{ext}: {shipped:?}");
        let empty = Manifest::default();
        let problems = empty.coverage(&shipped, &Default::default());
        assert!(
            problems
                .iter()
                .any(|p| p.contains(&format!("deploy/systemd/x.{ext}"))),
            "{ext}: {problems:?}"
        );
    }
}

#[test]
fn an_untracked_unit_file_is_not_shipped() {
    let dir = repo_with(
        &[("deploy/systemd/a.service", "[Unit]\n")],
        &[("deploy/systemd/scratch.timer", "[Timer]\n")],
    );
    let shipped = super::scan::unit_files(dir.path()).unwrap();
    assert_eq!(shipped, set(&["systemd/a.service"]));
    // A manifest entry for the untracked file does not count as covering it.
    let m = Manifest::parse(
        "[[unit]]\nname='s'\nfiles=['scratch.timer']\nhosts=['all']\nfirst_run='x'\n",
    )
    .unwrap();
    let problems = m.coverage(&shipped, &Default::default());
    assert!(problems
        .iter()
        .any(|p| p.contains("deploy/systemd/scratch.timer") && p.contains("not a tracked file")));
}

#[test]
fn a_tracked_file_missing_from_the_working_tree_is_an_error() {
    let dir = repo_with(&[("deploy/systemd/a.service", "[Unit]\n")], &[]);
    std::fs::remove_file(dir.path().join("deploy/systemd/a.service")).unwrap();
    let err = super::scan::unit_files(dir.path()).unwrap_err();
    assert!(err.contains("tracked but missing"), "{err}");
}

#[test]
fn a_directory_that_is_not_a_repository_is_an_error() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(dir.path().join("deploy")).unwrap();
    assert!(super::scan::unit_files(dir.path()).is_err());
}

#[test]
fn a_manifest_entry_whose_source_file_is_gone_fails_coverage() {
    let dir = repo_with(&[("deploy/systemd/a.service", "[Unit]\n")], &[]);
    let shipped = super::scan::unit_files(dir.path()).unwrap();
    let m = Manifest::parse(
        "[[unit]]\nname='a'\nfiles=['a.service','gone.timer']\nenable='gone.timer'\nhosts=['all']\nfirst_run='x'\n",
    )
    .unwrap();
    let problems = m.coverage(&shipped, &Default::default());
    assert_eq!(problems.len(), 1, "{problems:?}");
    assert!(
        problems[0].contains("deploy/systemd/gone.timer")
            && problems[0].contains("not a tracked file")
    );
}

#[test]
fn tracked_units_at_every_depth() {
    let dir = repo_with(
        &[
            ("deploy/a/b/deep.timer", "[Timer]\n"),
            ("deploy/top.service", "[Service]\n"),
            ("deploy/systemd/top.service", "[Service]\n"),
            ("deploy/ctx/ctx.timer.in", "[Timer]\n"),
            ("deploy/systemd/x.service.d/kg.conf", "[Service]\n"),
            ("deploy/systemd/x.service.example", "[Service]\n"),
        ],
        &[],
    );
    let shipped = super::scan::unit_files(dir.path()).unwrap();
    assert_eq!(
        shipped.iter().map(String::as_str).collect::<Vec<_>>(),
        [
            "a/b/deep.timer",
            "ctx/ctx.timer.in",
            "systemd/top.service",
            "top.service"
        ]
    );
    // The same set drives coverage: every depth is named, and each name is its own path.
    let none = Manifest::default().coverage(&shipped, &Default::default());
    assert_eq!(none.len(), 4, "{none:?}");
    let full = Manifest::parse(
        "[[unit]]\nname='d'\ndir='a/b'\nfiles=['deep.timer']\nnot_deployed='r'\n\
         [[unit]]\nname='c'\ndir='ctx'\nfiles=['ctx.timer.in']\nnot_deployed='r'\n\
         [[unit]]\nname='s'\nfiles=['top.service']\nnot_deployed='r'\n\
         [[unit]]\nname='r'\ndir='.'\nfiles=['top.service']\nnot_deployed='r'\n",
    )
    .unwrap();
    assert!(full.coverage(&shipped, &Default::default()).is_empty());
}

#[test]
fn one_entry_cannot_cover_the_same_name_in_two_directories() {
    let dir = repo_with(
        &[
            ("deploy/foo.service", "[Service]\n"),
            ("deploy/systemd/foo.service", "[Service]\n"),
        ],
        &[],
    );
    let shipped = super::scan::unit_files(dir.path()).unwrap();
    let m = Manifest::parse(
        "[[unit]]\nname='foo'\nfiles=['foo.service']\nhosts=['all']\nfirst_run='x'\n",
    )
    .unwrap();
    let problems = m.coverage(&shipped, &Default::default());
    assert_eq!(problems.len(), 1, "{problems:?}");
    assert!(problems[0].contains("deploy/foo.service"), "{problems:?}");
}

/// A temporary Cargo workspace: `members` as written in `[workspace]`, one library-less package per
/// `(path, extra Cargo.toml text, extra source files)`; every package gets `src/main.rs` unless it says
/// otherwise.
fn workspace(members: &str, crates: &[(&str, &str, &[&str])]) -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    let rust = dir.path().join("rust");
    std::fs::create_dir_all(&rust).unwrap();
    std::fs::write(
        rust.join("Cargo.toml"),
        format!(
            "[workspace]\nresolver = \"2\"\nmembers = {members}\nexclude = [\"crates/skipped\"]\n"
        ),
    )
    .unwrap();
    for (path, extra, sources) in crates {
        let crate_dir = rust.join(path);
        std::fs::create_dir_all(crate_dir.join("src/bin")).unwrap();
        let package = path.rsplit('/').next().unwrap();
        std::fs::write(
            crate_dir.join("Cargo.toml"),
            format!("[package]\nname = \"{package}\"\nversion = \"0.1.0\"\nedition = \"2021\"\n{extra}\n"),
        )
        .unwrap();
        for source in *sources {
            std::fs::write(crate_dir.join(source), "fn main() {}\n").unwrap();
        }
    }
    dir
}

#[test]
fn workspace_member_globs_classes_and_recursive_patterns_are_expanded_like_cargo_does() {
    let dir = workspace(
        "[\"crates/tool-[ab]\", \"deep/**/inner\", \"crates/s*\", \"plain\"]",
        &[
            ("crates/tool-a", "", &["src/main.rs"]),
            ("crates/tool-b", "", &["src/main.rs"]),
            ("crates/tool-c", "", &["src/main.rs"]),
            ("crates/skipped", "", &["src/main.rs"]),
            ("deep/x/y/inner", "", &["src/main.rs"]),
            ("plain", "", &["src/main.rs"]),
        ],
    );
    let bins = super::scan::bin_targets(dir.path()).unwrap();
    let names: Vec<&str> = bins.iter().map(|(_, b)| b.as_str()).collect();
    assert_eq!(
        names,
        ["inner", "plain", "tool-a", "tool-b"],
        "tool-c is no member, skipped is excluded"
    );
}

#[test]
fn unlisted_implicit_binary_fails_coverage() {
    let dir = workspace(
        "[\"auto\", \"manual\"]",
        &[
            ("auto", "", &["src/main.rs", "src/bin/extra.rs"]),
            (
                "manual",
                "autobins = false\n[[bin]]\nname = \"declared\"\npath = \"src/declared.rs\"\n",
                &["src/declared.rs", "src/bin/ignored.rs"],
            ),
        ],
    );
    let found = super::scan::bin_targets(dir.path()).unwrap();
    assert_eq!(
        found,
        bins(&[("auto", "auto"), ("auto", "extra"), ("manual", "declared")]),
        "src/main.rs and src/bin/* are implicit targets, autobins = false drops them"
    );
    let m = Manifest::parse(
        "[[binary]]\npackage='auto'\nbin='auto'\nnot_deployed='r'\n\
         [[binary]]\npackage='manual'\nbin='declared'\nnot_deployed='r'\n",
    )
    .unwrap();
    let problems = m.coverage(&Default::default(), &found);
    assert_eq!(problems.len(), 1, "{problems:?}");
    assert!(problems[0].contains("auto/extra"), "{problems:?}");
}
