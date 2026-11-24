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

pub(super) fn set(items: &[&str]) -> BTreeSet<String> {
    items.iter().map(|s| (*s).to_owned()).collect()
}

pub(super) fn bins(items: &[(&str, &str)]) -> BTreeSet<(String, String)> {
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
    let env = "[[unit]]\nname='x'\nfiles=['x.service']\nhosts=['all']\nfirst_run='c'\nenv_files=['/etc/../x.env']\n";
    assert!(Manifest::parse(env)
        .unwrap_err()
        .contains("must be a file name"));
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
fn units_at_the_deploy_root_and_in_systemd_are_distinct_and_both_declarable() {
    let root_only =
        "[[unit]]\nname='r'\ndir='.'\nfiles=['foo.service']\nnot_deployed='root unit'\n";
    let m = Manifest::parse(root_only).unwrap();
    assert_eq!(m.unit[0].coverage_key("foo.service"), "foo.service");
    assert_eq!(m.unit[0].source_dir(), "deploy");
    let both = format!(
        "{root_only}[[unit]]\nname='s'\nfiles=['foo.service']\nnot_deployed='systemd unit'\n"
    );
    let m = Manifest::parse(&both).unwrap();
    let shipped = set(&["foo.service", "systemd/foo.service"]);
    assert!(m.coverage(&shipped, &Default::default()).is_empty());
    let only_systemd =
        Manifest::parse("[[unit]]\nname='s'\nfiles=['foo.service']\nnot_deployed='systemd unit'\n")
            .unwrap();
    let problems = only_systemd.coverage(&shipped, &Default::default());
    assert_eq!(problems.len(), 1, "{problems:?}");
    assert!(problems[0].contains("deploy/foo.service"), "{problems:?}");
    let twice = "[[unit]]\nname='a'\ndir='.'\nfiles=['x.service']\nnot_deployed='r'\n\
                 [[unit]]\nname='b'\ndir='.'\nfiles=['x.service']\nnot_deployed='r'\n";
    assert!(Manifest::parse(twice).unwrap_err().contains("two entries"));
    for bad in ["", "..", "a/../b", "/abs", "a/"] {
        let text =
            format!("[[unit]]\nname='x'\ndir='{bad}'\nfiles=['x.service']\nnot_deployed='r'\n");
        assert!(
            Manifest::parse(&text)
                .unwrap_err()
                .contains("relative path"),
            "{bad:?}"
        );
    }
}

#[test]
fn a_template_family_enables_its_timer_template_and_needs_an_installer() {
    let text = "[[unit]]\nname='t'\ndir='ctx'\nfiles=['t.service.in','t.timer.in']\nhosts=['all']\nfirst_run='x'\n\
                [unit.installer]\nvia='deploy/bootstrap.sh'\nstep='render t'\nrenders=['t.service','t.timer']\nenable='t.timer'\n";
    let m = Manifest::parse(text).unwrap();
    // The source files and the installed units are named separately.
    assert_eq!(m.unit[0].enable_unit().unwrap(), "t.timer.in");
    assert_eq!(m.unit[0].service_unit().as_deref(), Some("t.service.in"));
    assert_eq!(m.unit[0].installed_enable_unit().unwrap(), "t.timer");
    assert_eq!(
        m.unit[0].installed_service_unit().as_deref(),
        Some("t.service")
    );
    let installer = |extra: &str| {
        format!(
            "[[unit]]\nname='t'\nfiles=['t.service']\nhosts=['all']\nfirst_run='x'\n[unit.installer]\n{extra}\n"
        )
    };
    for (extra, want) in [
        (
            "via='/abs'\nstep='s'\nrenders=['t.service']\nenable='t.service'",
            "repository file path",
        ),
        (
            "via='a/../b'\nstep='s'\nrenders=['t.service']\nenable='t.service'",
            "repository file path",
        ),
        (
            "via='b.sh'\nstep=' '\nrenders=['t.service']\nenable='t.service'",
            "step",
        ),
        (
            "via='b.sh'\nstep='s'\nrenders=[]\nenable='t.service'",
            "step",
        ),
        (
            "via='b.sh'\nstep='s'\nrenders=['t.conf']\nenable='t.conf'",
            "unit file names",
        ),
    ] {
        let err = Manifest::parse(&installer(extra)).unwrap_err();
        assert!(err.contains(want), "{extra}: {err}");
    }
}

#[test]
fn env_files_may_be_names_or_absolute_or_home_paths() {
    let with = |env: &str| {
        format!("[[unit]]\nname='x'\nfiles=['x.service']\nhosts=['all']\nfirst_run='c'\nenv_files=['{env}']\n")
    };
    for ok in [
        "a.env",
        "sub/%i.env",
        "/srv/other/c.env",
        "%h/other/c.env",
        "a.env|/srv/b.env",
    ] {
        assert!(Manifest::parse(&with(ok)).is_ok(), "{ok}");
    }
    for bad in [
        "/srv/../c.env",
        "c.conf",
        "//c.env",
        "a b.env",
        "a=b.env",
        "%h/../c.env",
    ] {
        assert!(
            Manifest::parse(&with(bad))
                .unwrap_err()
                .contains("env file"),
            "{bad}"
        );
    }
}
