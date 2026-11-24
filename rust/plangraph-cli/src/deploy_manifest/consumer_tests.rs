use super::scan::Consumer;
use super::scan_tests::repo_with;
use super::*;

fn consumers(items: &[(&str, &str, &str)]) -> BTreeSet<Consumer> {
    items
        .iter()
        .map(|(unit, bin, path)| Consumer {
            unit: (*unit).to_owned(),
            bin: (*bin).to_owned(),
            path: (*path).to_owned(),
        })
        .collect()
}

const CONSUMERS: &str = "\
[[unit]]\nname='keep'\nfiles=['keep.service']\nhosts=['keeper']\nfirst_run='c'\n\
[[unit]]\nname='wsl'\nfiles=['wsl.service']\nhosts=['wsl']\nfirst_run='c'\n\
[[unit]]\nname='off'\nfiles=['off.service']\nnot_deployed='r'\n\
[[binary]]\npackage='p'\nbin='tool'\nhosts=['all']\nfirst_run='c'\n\
install_paths=[{path='~/.local/bin/tool',hosts=['keeper']},{path='/usr/local/bin/tool',hosts=['wsl']}]\n\
[[binary]]\npackage='p'\nbin='plain'\nhosts=['keeper']\nfirst_run='c'\n";

#[test]
fn a_binary_run_from_a_path_its_entry_does_not_declare_is_named() {
    let m = Manifest::parse(CONSUMERS).unwrap();
    let ok = consumers(&[
        ("systemd/keep.service", "tool", "~/.local/bin/tool"),
        ("systemd/wsl.service", "tool", "/usr/local/bin/tool"),
        ("systemd/keep.service", "plain", "~/.local/bin/plain"),
        ("systemd/keep.service", "python3", "/usr/bin/python3"),
    ]);
    assert_eq!(m.consumer_problems(&ok), Vec::<String>::new());
    // A path outside the two usual directories is judged like any other.
    let opt = consumers(&[("systemd/keep.service", "tool", "/opt/plangraph/bin/tool")]);
    let problems = m.consumer_problems(&opt);
    assert_eq!(problems.len(), 1, "{problems:?}");
    assert!(
        problems[0].contains("/opt/plangraph/bin/tool") && problems[0].contains("install_paths")
    );
    let bad = consumers(&[
        ("systemd/keep.service", "plain", "/usr/local/bin/plain"),
        ("systemd/off.service", "tool", "/anywhere/tool"),
    ]);
    assert_eq!(m.consumer_problems(&bad).len(), 2);
}

#[test]
fn a_unit_on_a_role_the_binary_is_not_installed_for_is_named() {
    let m = Manifest::parse(CONSUMERS).unwrap();
    // `tool` at /usr/local/bin is for wsl hosts only; a keeper unit running it there has none.
    let wrong_role = consumers(&[("systemd/keep.service", "tool", "/usr/local/bin/tool")]);
    let problems = m.consumer_problems(&wrong_role);
    assert_eq!(problems.len(), 1, "{problems:?}");
    assert!(problems[0].contains("on role keeper"), "{problems:?}");
    // `plain` is installed on keeper hosts only; a wsl unit running it has no binary.
    let off_host = consumers(&[("systemd/wsl.service", "plain", "~/.local/bin/plain")]);
    assert!(m.consumer_problems(&off_host)[0].contains("on role wsl"));
}

#[test]
fn consumers_and_env_files_are_read_from_the_unit_files() {
    let dir = repo_with(
        &[
            (
                "deploy/systemd/a.service",
                "[Service]\nEnvironmentFile=-%h/.config/plangraph/a.env\nEnvironmentFile=/etc/plangraph/b.env\n\
                 ExecStartPre=-%h/.local/bin/pre --x\nExecStart=/usr/local/bin/tool run\n\
                 ExecStart=/usr/bin/python3 -m x\nExecStopPost=-/opt/x/bin/post\nExecReload=%h/.local/bin/reload\n\
                 ExecStart=relative/not-a-path\nEnvironmentFile=-/srv/other/c.env\nEnvironmentFile=\n",
            ),
            ("deploy/systemd/t@.service", "[Service]\nEnvironmentFile=%h/.config/plangraph/sub/%i.env\n"),
        ],
        &[],
    );
    let shipped = super::scan::unit_files(dir.path()).unwrap();
    let found = super::scan::consumers(dir.path(), &shipped).unwrap();
    let a = "systemd/a.service";
    assert_eq!(
        found,
        consumers(&[
            (a, "post", "/opt/x/bin/post"),
            (a, "pre", "~/.local/bin/pre"),
            (a, "python3", "/usr/bin/python3"),
            (a, "reload", "~/.local/bin/reload"),
            (a, "tool", "/usr/local/bin/tool"),
        ])
    );
    let env = super::scan::unit_env_files(dir.path(), &shipped).unwrap();
    assert_eq!(
        env,
        [
            ("systemd/a.service", "a.env"),
            ("systemd/a.service", "b.env"),
            ("systemd/a.service", "/srv/other/c.env"),
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
    assert_eq!(problems.len(), 2, "{problems:?}");
    assert!(problems
        .iter()
        .any(|p| p.contains("b.env") && p.contains("unit a")));
    assert!(problems.iter().any(|p| p.contains("/srv/other/c.env")));
    // Declaring the external path and the other name clears both: the manifest can say what the unit reads.
    let declared = Manifest::parse(
        "[[unit]]\nname='a'\nfiles=['a.service']\nhosts=['all']\nfirst_run='x'\n\
         env_files=['a.env','b.env','/srv/other/c.env']\n\
         [[unit]]\nname='t'\nfiles=['t@.service']\nenable='t@.service'\nnot_deployed='r'\nenv_files=['sub/%i.env']\n",
    )
    .unwrap();
    assert_eq!(declared.env_problems(&env), Vec::<String>::new());
}

#[test]
fn quoted_and_prefixed_exec_programs_are_read_as_systemd_reads_them() {
    let dir = repo_with(
        &[(
            "deploy/systemd/q.service",
            "[Service]\nExecStart=:/opt/bin/pg-spawnd run\nExecStartPre=-\"/opt/my tools/pg-spawnd\" --x\n\
             ExecStartPost=@+!/opt/bin/other arg0\nExecStop='/opt/sq dir/stopper' now\n\
             ExecReload=\"/opt/esc\\\"d/x\" y\nEnvironmentFile=-\"/srv/q dir/x.env\"\n",
        )],
        &[],
    );
    let shipped = super::scan::unit_files(dir.path()).unwrap();
    let found = super::scan::consumers(dir.path(), &shipped).unwrap();
    let paths: Vec<&str> = found.iter().map(|c| c.path.as_str()).collect();
    assert_eq!(
        paths,
        [
            "/opt/bin/other",
            "/opt/bin/pg-spawnd",
            "/opt/my tools/pg-spawnd",
            "/opt/sq dir/stopper",
            "/opt/esc\"d/x"
        ]
    );
    // From the unit file through manifest validation: undeclared paths are named, declared ones pass.
    let head = "[[unit]]\nname='q'\nfiles=['q.service']\nhosts=['keeper']\nfirst_run='c'\n\
                [[binary]]\npackage='p'\nbin='pg-spawnd'\nhosts=['keeper']\nfirst_run='c'\n";
    let bare = Manifest::parse(head).unwrap();
    let problems = bare.consumer_problems(&found);
    assert_eq!(problems.len(), 2, "{problems:?}");
    assert!(problems.iter().any(|p| p.contains("/opt/bin/pg-spawnd")));
    assert!(problems
        .iter()
        .any(|p| p.contains("/opt/my tools/pg-spawnd")));
    let declared = Manifest::parse(&format!(
        "{head}install_paths=[{{path='/opt/bin/pg-spawnd',hosts=['keeper']}},{{path='/opt/my tools/pg-spawnd',hosts=['keeper']}}]\n"
    ))
    .unwrap();
    assert_eq!(declared.consumer_problems(&found), Vec::<String>::new());
    let env = super::scan::unit_env_files(dir.path(), &shipped).unwrap();
    assert!(env.contains(&(
        "systemd/q.service".to_owned(),
        "/srv/q dir/x.env".to_owned()
    )));
}

#[test]
fn a_template_without_an_installer_or_with_a_missing_installer_script_is_named() {
    let dir = repo_with(
        &[
            ("deploy/ctx/c.service.in", "[Service]\n"),
            ("deploy/bootstrap.sh", "#!/bin/sh\n"),
        ],
        &[],
    );
    let shipped = super::scan::unit_files(dir.path()).unwrap();
    let head =
        "[[unit]]\nname='c'\ndir='ctx'\nfiles=['c.service.in']\nhosts=['all']\nfirst_run='x'\n";
    let bare = Manifest::parse(head).unwrap();
    let problems = bare.installer_problems(dir.path(), &shipped);
    assert_eq!(problems.len(), 1, "{problems:?}");
    assert!(problems[0].contains("deploy/ctx/c.service.in") && problems[0].contains("installer"));
    let with = |via: &str| {
        Manifest::parse(&format!(
            "{head}[unit.installer]\nvia='{via}'\nstep='render c'\nrenders=['c.service']\nenable='c.service'\n"
        ))
        .unwrap()
    };
    assert!(with("deploy/bootstrap.sh")
        .installer_problems(dir.path(), &shipped)
        .is_empty());
    let missing = with("deploy/missing.sh").installer_problems(dir.path(), &shipped);
    assert_eq!(missing.len(), 1, "{missing:?}");
    assert!(missing[0].contains("not a tracked file"));
}

#[test]
fn a_placeholder_unit_needs_an_installer_like_a_template_does() {
    let dir = repo_with(
        &[
            (
                "deploy/w/stall.service",
                "[Service]\nExecStart=\"@PYTHON@\" -m x --root @ROOT@\n",
            ),
            (
                "deploy/w/plain.service",
                "[Service]\nExecStart=/bin/true\nDescription=mail a@b.example\n",
            ),
            ("deploy/bootstrap.sh", "#!/bin/sh\n"),
        ],
        &[],
    );
    let shipped = super::scan::unit_files(dir.path()).unwrap();
    let head = "[[unit]]\nname='s'\ndir='w'\nfiles=['stall.service']\nhosts=['all']\nfirst_run='x'\n\
                [[unit]]\nname='p'\ndir='w'\nfiles=['plain.service']\nhosts=['all']\nfirst_run='x'\n";
    let bare = Manifest::parse(head).unwrap();
    let problems = bare.installer_problems(dir.path(), &shipped);
    assert_eq!(problems.len(), 1, "{problems:?}");
    assert!(
        problems[0].contains("deploy/w/stall.service"),
        "{problems:?}"
    );
    // The installer goes on the stall entry; the unit with no placeholder needs none.
    let fixed = head.replacen(
        "files=['stall.service']\nhosts=['all']\nfirst_run='x'\n",
        "files=['stall.service']\nhosts=['all']\nfirst_run='x'\n\
         [unit.installer]\nvia='deploy/bootstrap.sh'\nstep='render'\nrenders=['plangraph-stall.service']\nenable='plangraph-stall.service'\n",
        1,
    );
    // `[unit.installer]` binds to the entry above it, so the plain entry follows as a new table.
    let m = Manifest::parse(&fixed).unwrap();
    assert_eq!(
        m.installer_problems(dir.path(), &shipped),
        Vec::<String>::new()
    );
}

#[test]
fn an_untracked_installer_script_does_not_satisfy_via() {
    let dir = repo_with(
        &[("deploy/c/c.service.in", "[Service]\n")],
        &[("deploy/bootstrap.sh", "#!/bin/sh\n")],
    );
    assert!(dir.path().join("deploy/bootstrap.sh").is_file());
    let shipped = super::scan::unit_files(dir.path()).unwrap();
    let m = Manifest::parse(
        "[[unit]]\nname='c'\ndir='c'\nfiles=['c.service.in']\nenable='c.service.in'\nhosts=['all']\nfirst_run='x'\n\
         [unit.installer]\nvia='deploy/bootstrap.sh'\nstep='render'\nrenders=['c.service']\nenable='c.service'\n",
    )
    .unwrap();
    let problems = m.installer_problems(dir.path(), &shipped);
    assert_eq!(problems.len(), 1, "{problems:?}");
    assert!(problems[0].contains("not a tracked file"), "{problems:?}");
    // Staging the script is what makes it a repository file.
    let status = std::process::Command::new("git")
        .arg("-C")
        .arg(dir.path())
        .args(["add", "deploy/bootstrap.sh"])
        .status()
        .unwrap();
    assert!(status.success());
    assert_eq!(
        m.installer_problems(dir.path(), &shipped),
        Vec::<String>::new()
    );
}
