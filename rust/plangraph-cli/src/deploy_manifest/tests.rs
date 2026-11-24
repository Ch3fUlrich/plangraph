use super::*;

const GOOD: &str = r#"
[[unit]]
name = "pg-a"
files = ["pg-a.service", "pg-a.timer"]
hosts = ["keeper"]
env_files = ["a.env"]
first_run = "journalctl shows a clean run"

[[unit]]
name = "pg-b"
files = ["pg-b.service"]
not_deployed = "example only"

[[binary]]
package = "p"
bin = "tool"
hosts = ["all"]
first_run = "tool --version"

[[binary]]
package = "p"
bin = "helper"
not_deployed = "test helper"
"#;

fn set(items: &[&str]) -> BTreeSet<String> {
    items.iter().map(|s| (*s).to_owned()).collect()
}

/// A temporary repository with `tracked` committed to the index and `untracked` only on disk.
fn repo_with(tracked: &[(&str, &str)], untracked: &[(&str, &str)]) -> tempfile::TempDir {
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

fn bins(items: &[(&str, &str)]) -> BTreeSet<(String, String)> {
    items
        .iter()
        .map(|(p, b)| ((*p).to_owned(), (*b).to_owned()))
        .collect()
}

#[test]
fn a_complete_manifest_covers_the_tree() {
    let m = Manifest::parse(GOOD).unwrap();
    let units = set(&[
        "systemd/pg-a.service",
        "systemd/pg-a.timer",
        "systemd/pg-b.service",
    ]);
    assert!(m
        .coverage(&units, &bins(&[("p", "tool"), ("p", "helper")]))
        .is_empty());
    assert_eq!(m.unit[0].enable_unit().unwrap(), "pg-a.timer");
    assert_eq!(m.unit[0].service_unit().as_deref(), Some("pg-a.service"));
}

#[test]
fn a_unit_file_missing_from_the_manifest_is_named() {
    let m = Manifest::parse(GOOD).unwrap();
    let units = set(&[
        "systemd/pg-a.service",
        "systemd/pg-a.timer",
        "systemd/pg-b.service",
        "systemd/pg-new.timer",
    ]);
    let problems = m.coverage(&units, &bins(&[("p", "tool"), ("p", "helper")]));
    assert_eq!(problems.len(), 1, "{problems:?}");
    assert!(problems[0].contains("pg-new.timer") && problems[0].contains("not_deployed"));
}

#[test]
fn a_binary_target_missing_from_the_manifest_is_named() {
    let m = Manifest::parse(GOOD).unwrap();
    let units = set(&[
        "systemd/pg-a.service",
        "systemd/pg-a.timer",
        "systemd/pg-b.service",
    ]);
    let problems = m.coverage(
        &units,
        &bins(&[("p", "tool"), ("p", "helper"), ("q", "new-bin")]),
    );
    assert_eq!(problems.len(), 1, "{problems:?}");
    assert!(problems[0].contains("q/new-bin"));
}

#[test]
fn an_entry_for_something_that_does_not_exist_is_named() {
    let m = Manifest::parse(GOOD).unwrap();
    let problems = m.coverage(
        &set(&["systemd/pg-a.service", "systemd/pg-a.timer"]),
        &bins(&[("p", "tool")]),
    );
    assert_eq!(problems.len(), 2, "{problems:?}");
}

#[test]
fn an_entry_needs_hosts_and_a_first_run_check_or_a_reason() {
    let no_hosts = "[[unit]]\nname='x'\nfiles=['x.service']\nfirst_run='c'\n";
    assert!(Manifest::parse(no_hosts)
        .unwrap_err()
        .contains("not_deployed"));
    let no_check = "[[unit]]\nname='x'\nfiles=['x.service']\nhosts=['all']\n";
    assert!(Manifest::parse(no_check).unwrap_err().contains("first_run"));
    let blank = "[[unit]]\nname='x'\nfiles=['x.service']\nnot_deployed='  '\n";
    assert!(Manifest::parse(blank).unwrap_err().contains("reason"));
    let both = "[[binary]]\npackage='p'\nbin='b'\nhosts=['all']\nnot_deployed='r'\n";
    assert!(Manifest::parse(both).unwrap_err().contains("excludes"));
}

#[test]
fn roles_env_files_and_duplicates_are_validated() {
    let role = "[[binary]]\npackage='p'\nbin='b'\nhosts=['coding']\nfirst_run='c'\n";
    assert!(Manifest::parse(role)
        .unwrap_err()
        .contains("unknown host role"));
    let env = "[[unit]]\nname='x'\nfiles=['x.service']\nhosts=['all']\nfirst_run='c'\nenv_files=['/etc/x.env']\n";
    assert!(Manifest::parse(env)
        .unwrap_err()
        .contains("relative file name"));
    let twice = "[[unit]]\nname='x'\nfiles=['x.service']\nnot_deployed='r'\n[[unit]]\nname='y'\nfiles=['x.service']\nnot_deployed='r'\n";
    assert!(Manifest::parse(twice).unwrap_err().contains("two entries"));
    let two_timers = "[[unit]]\nname='x'\nfiles=['a.timer','b.timer']\nnot_deployed='r'\n";
    assert!(Manifest::parse(two_timers).unwrap_err().contains("enable"));
}

#[test]
fn selection_follows_the_roles_of_the_host() {
    let m = Manifest::parse(GOOD).unwrap();
    assert_eq!(m.binaries_for(&set(&[])), vec![("p", "tool")]);
    assert_eq!(m.units_for(&set(&[])).len(), 0);
    assert_eq!(m.units_for(&set(&["keeper"])).len(), 1);
    assert_eq!(m.units_for(&set(&["worker"])).len(), 0);
}

#[test]
fn host_roles_come_from_the_env_then_the_file_and_reject_typos() {
    assert_eq!(
        host_roles(Some("keeper, wsl"), Some("worker")).unwrap(),
        set(&["keeper", "wsl"])
    );
    assert_eq!(
        host_roles(None, Some("worker\n")).unwrap(),
        set(&["worker"])
    );
    assert!(host_roles(None, None).unwrap().is_empty());
    assert!(host_roles(Some("keper"), None).is_err());
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
fn the_scan_lists_tracked_files_at_any_depth_with_their_directory() {
    let dir = repo_with(
        &[
            ("deploy/a/b/deep.timer", "[Timer]\n"),
            ("deploy/top.service", "[Service]\n"),
            ("deploy/systemd/top.service", "[Service]\n"),
            ("deploy/systemd/x.service.d/kg.conf", "[Service]\n"),
        ],
        &[],
    );
    let shipped = super::scan::unit_files(dir.path()).unwrap();
    assert_eq!(
        shipped.iter().map(String::as_str).collect::<Vec<_>>(),
        ["a/b/deep.timer", "systemd/top.service", "top.service"]
    );
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
    // Naming the same file name in both directories takes two entries, one per directory.
    let both = Manifest::parse(
        "[[unit]]\nname='a'\nfiles=['foo.service']\nhosts=['all']\nfirst_run='x'\n\
         [[unit]]\nname='b'\ndir='.'\nfiles=['foo.service']\nhosts=['all']\nfirst_run='x'\n",
    );
    assert!(both.unwrap_err().contains("relative path"));
    let m = Manifest::parse(
        "[[unit]]\nname='a'\nfiles=['foo.service']\nhosts=['all']\nfirst_run='x'\n\
         [[unit]]\nname='b'\ndir='other'\nfiles=['foo.service']\nhosts=['all']\nfirst_run='x'\n",
    )
    .unwrap();
    assert_eq!(
        m.coverage(&shipped, &Default::default()).len(),
        2,
        "other/foo.service is not top-level foo.service, and deploy/foo.service is still unnamed"
    );
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
fn a_not_deployed_entry_passes_only_with_a_reason() {
    let units = set(&["systemd/x.service"]);
    let with = Manifest::parse(
        "[[unit]]\nname='x'\nfiles=['x.service']\nnot_deployed='needs a collector'\n",
    )
    .unwrap();
    assert!(with.coverage(&units, &Default::default()).is_empty());
    for blank in ["''", "'   '"] {
        let text = format!("[[unit]]\nname='x'\nfiles=['x.service']\nnot_deployed={blank}\n");
        assert!(
            Manifest::parse(&text).unwrap_err().contains("reason"),
            "{blank}"
        );
        let bin = format!("[[binary]]\npackage='p'\nbin='b'\nnot_deployed={blank}\n");
        assert!(
            Manifest::parse(&bin).unwrap_err().contains("reason"),
            "{blank}"
        );
    }
}

#[test]
fn a_binary_declared_with_two_install_paths_parses() {
    let text = "[[binary]]\npackage='p'\nbin='tool'\nhosts=['all']\nfirst_run='c'\n\
        install_paths=[{path='/usr/local/bin/tool',hosts=['keeper']},{path='~/.local/bin/tool',hosts=['worker','wsl']}]\n";
    let m = Manifest::parse(text).unwrap();
    let binary = &m.binary[0];
    assert_eq!(binary.install_paths.len(), 2);
    assert_eq!(
        binary.install_paths_on(&set(&["keeper"])),
        ["/usr/local/bin/tool"]
    );
    assert_eq!(
        binary.install_paths_on(&set(&["wsl"])),
        ["~/.local/bin/tool"]
    );
    assert!(binary.install_paths_on(&set(&[])).is_empty());
}

#[test]
fn install_paths_are_validated() {
    let head = "[[binary]]\npackage='p'\nbin='b'\nhosts=['all']\nfirst_run='c'\n";
    for (paths, want) in [
        ("[{path='bin/b',hosts=['all']}]", "absolute"),
        ("[{path='/usr/../b',hosts=['all']}]", "absolute"),
        ("[{path='/usr/bin/',hosts=['all']}]", "absolute"),
        ("[{path='/usr/bin/b',hosts=[]}]", "no hosts"),
        (
            "[{path='/usr/bin/b',hosts=['coding']}]",
            "unknown host role",
        ),
        (
            "[{path='/a/b',hosts=['all']},{path='/a/b',hosts=['keeper']}]",
            "twice",
        ),
    ] {
        let err = Manifest::parse(&format!("{head}install_paths={paths}\n")).unwrap_err();
        assert!(err.contains(want), "{paths}: {err}");
    }
    let old = format!("{head}install_path='/usr/local/bin/b'\n");
    assert!(
        Manifest::parse(&old).is_err(),
        "the single install_path is gone"
    );
    let excluded = "[[binary]]\npackage='p'\nbin='b'\nnot_deployed='r'\ninstall_paths=[{path='/a/b',hosts=['all']}]\n";
    assert!(Manifest::parse(excluded).unwrap_err().contains("excludes"));
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

fn workspace(members: &str, crates: &[&str]) -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    let rust = dir.path().join("rust");
    std::fs::create_dir_all(&rust).unwrap();
    std::fs::write(
        rust.join("Cargo.toml"),
        format!("[workspace]\nmembers = {members}\nexclude = [\"crates/skipped\"]\n"),
    )
    .unwrap();
    for name in crates {
        let crate_dir = rust.join(name);
        std::fs::create_dir_all(crate_dir.join("src")).unwrap();
        let package = name.rsplit('/').next().unwrap();
        std::fs::write(
            crate_dir.join("Cargo.toml"),
            format!("[package]\nname = \"{package}\"\n"),
        )
        .unwrap();
        std::fs::write(crate_dir.join("src/main.rs"), "fn main() {}\n").unwrap();
    }
    dir
}

#[test]
fn workspace_member_globs_are_expanded_like_cargo_does() {
    let dir = workspace(
        "[\"crates/*\", \"tool-?\", \"plain\"]",
        &[
            "crates/one",
            "crates/two",
            "crates/skipped",
            "tool-a",
            "plain",
        ],
    );
    let bins = super::scan::bin_targets(dir.path()).unwrap();
    let names: Vec<&str> = bins.iter().map(|(_, b)| b.as_str()).collect();
    assert_eq!(
        names,
        ["one", "plain", "tool-a", "two"],
        "skipped is excluded"
    );
    let none = workspace("[\"nothing/*\"]", &["other"]);
    assert!(super::scan::bin_targets(none.path()).is_err());
}

#[test]
fn a_binary_run_from_a_path_its_entry_does_not_declare_is_named() {
    let m = Manifest::parse(
        "[[binary]]\npackage='p'\nbin='tool'\nhosts=['all']\nfirst_run='c'\n\
         install_paths=[{path='/usr/local/bin/tool',hosts=['keeper']}]\n\
         [[binary]]\npackage='p'\nbin='plain'\nhosts=['all']\nfirst_run='c'\n",
    )
    .unwrap();
    let ok = [
        ("tool", "/usr/local/bin/tool"),
        ("plain", "~/.local/bin/plain"),
        ("python", "/usr/local/bin/python"),
    ]
    .iter()
    .map(|(b, p)| ((*b).to_owned(), (*p).to_owned()))
    .collect();
    assert!(m.consumer_problems(&ok).is_empty());
    let bad = [
        ("tool", "~/.local/bin/tool"),
        ("plain", "/usr/local/bin/plain"),
    ]
    .iter()
    .map(|(b, p)| ((*b).to_owned(), (*p).to_owned()))
    .collect();
    let problems = m.consumer_problems(&bad);
    assert_eq!(problems.len(), 2, "{problems:?}");
}

#[test]
fn consumer_paths_and_env_files_are_read_from_the_unit_files() {
    let dir = repo_with(
        &[
            (
                "deploy/systemd/a.service",
                "[Service]\nEnvironmentFile=-%h/.config/plangraph/a.env\nEnvironmentFile=/etc/plangraph/b.env\n\
                 ExecStartPre=-%h/.local/bin/pre --x\nExecStart=/usr/local/bin/tool run\n\
                 ExecStart=/usr/bin/python3 -m x\n",
            ),
            ("deploy/systemd/t@.service", "[Service]\nEnvironmentFile=%h/.config/plangraph/sub/%i.env\n"),
        ],
        &[],
    );
    let shipped = super::scan::unit_files(dir.path()).unwrap();
    let consumers = super::scan::consumer_paths(dir.path(), &shipped).unwrap();
    assert_eq!(
        consumers,
        [("pre", "~/.local/bin/pre"), ("tool", "/usr/local/bin/tool")]
            .iter()
            .map(|(b, p)| ((*b).to_owned(), (*p).to_owned()))
            .collect()
    );
    let env = super::scan::unit_env_files(dir.path(), &shipped).unwrap();
    assert_eq!(
        env,
        [
            ("systemd/a.service", "a.env"),
            ("systemd/a.service", "b.env"),
            ("systemd/t@.service", "sub/%i.env")
        ]
        .iter()
        .map(|(u, e)| ((*u).to_owned(), (*e).to_owned()))
        .collect()
    );
    let m = Manifest::parse(
        "[[unit]]\nname='a'\nfiles=['a.service']\nhosts=['all']\nfirst_run='x'\nenv_files=['a.env']\n\
         [[unit]]\nname='t'\nfiles=['t@.service']\nenable='t@.service'\nnot_deployed='r'\nenv_files=['sub/%i.env']\n",
    )
    .unwrap();
    let problems = m.env_problems(&env);
    assert_eq!(problems.len(), 1, "{problems:?}");
    assert!(problems[0].contains("b.env") && problems[0].contains("unit a"));
}
