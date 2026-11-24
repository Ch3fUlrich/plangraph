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

fn bins(items: &[(&str, &str)]) -> BTreeSet<(String, String)> {
    items
        .iter()
        .map(|(p, b)| ((*p).to_owned(), (*b).to_owned()))
        .collect()
}

#[test]
fn a_complete_manifest_covers_the_tree() {
    let m = Manifest::parse(GOOD).unwrap();
    let units = set(&["pg-a.service", "pg-a.timer", "pg-b.service"]);
    assert!(m
        .coverage(&units, &bins(&[("p", "tool"), ("p", "helper")]))
        .is_empty());
    assert_eq!(m.unit[0].enable_unit().unwrap(), "pg-a.timer");
    assert_eq!(m.unit[0].service_unit().as_deref(), Some("pg-a.service"));
}

#[test]
fn a_unit_file_missing_from_the_manifest_is_named() {
    let m = Manifest::parse(GOOD).unwrap();
    let units = set(&["pg-a.service", "pg-a.timer", "pg-b.service", "pg-new.timer"]);
    let problems = m.coverage(&units, &bins(&[("p", "tool"), ("p", "helper")]));
    assert_eq!(problems.len(), 1, "{problems:?}");
    assert!(problems[0].contains("pg-new.timer") && problems[0].contains("not_deployed"));
}

#[test]
fn a_binary_target_missing_from_the_manifest_is_named() {
    let m = Manifest::parse(GOOD).unwrap();
    let units = set(&["pg-a.service", "pg-a.timer", "pg-b.service"]);
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
        &set(&["pg-a.service", "pg-a.timer"]),
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
    let dir = tempfile::tempdir().unwrap();
    let deploy = dir.path().join("deploy");
    for (sub, file) in [
        ("systemd", "a.service"),
        ("liveness", "w@.service"),
        ("pganchor", "p.socket"),
    ] {
        std::fs::create_dir_all(deploy.join(sub)).unwrap();
        std::fs::write(deploy.join(sub).join(file), "[Unit]\n").unwrap();
    }
    std::fs::write(deploy.join("liveness/router.toml"), "x = 1\n").unwrap();
    let shipped = super::scan::unit_files(dir.path()).unwrap();
    assert_eq!(
        shipped.iter().map(String::as_str).collect::<Vec<_>>(),
        ["a.service", "liveness/w@.service", "pganchor/p.socket"]
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
