//! Unit file words read as systemd 255 reads them: C escapes assemble bytes, `\u` and `\U` are characters.
use super::scan_tests::repo_with;
use super::*;

fn units(lines: &str) -> (tempfile::TempDir, BTreeSet<String>) {
    let dir = repo_with(
        &[("deploy/systemd/u.service", &format!("[Service]\n{lines}"))],
        &[],
    );
    let shipped = super::scan::unit_files(dir.path()).unwrap();
    (dir, shipped)
}

fn manifest(bins: &[&str]) -> Manifest {
    let mut text =
        "[[unit]]\nname='u'\nfiles=['u.service']\nhosts=['keeper']\nfirst_run='c'\n".to_owned();
    for bin in bins {
        text.push_str(&format!(
            "[[binary]]\npackage='p'\nbin='{bin}'\nhosts=['keeper']\nfirst_run='c'\n"
        ));
    }
    Manifest::parse(&text).unwrap()
}

#[test]
fn unicode_and_byte_escapes_are_decoded_before_the_path_is_judged() {
    let (dir, shipped) = units(
        "ExecStart=\\u002fopt/bin/pg-spawnd\n\
         ExecStartPre=\"\\U0000002fopt/bin/wide\"\n\
         ExecStartPost=/opt/caf\\xc3\\xa9/bytes\n\
         ExecStop=/opt/caf\\303\\251/octal\n\
         ExecReload=/opt/caf\\u00e9/uni\n\
         ExecCondition=/opt/\\u00zz/short\n",
    );
    let found = super::scan::consumers(dir.path(), &shipped).unwrap();
    let paths: BTreeSet<&str> = found.iter().map(|c| c.path.as_str()).collect();
    assert_eq!(
        paths,
        BTreeSet::from([
            "/opt/bin/pg-spawnd",
            "/opt/bin/wide",
            "/opt/caf\u{e9}/bytes",
            "/opt/caf\u{e9}/octal",
            "/opt/caf\u{e9}/uni",
            "/opt/\\u00zz/short",
        ]),
        "xc3 xa9 is the one character U+00E9, not two"
    );
    // Through the manifest: the decoded path is named until the entry declares it.
    let m = manifest(&["pg-spawnd", "wide", "bytes", "octal", "uni", "short"]);
    let problems = m.consumer_problems(&found);
    assert_eq!(problems.len(), 6, "{problems:?}");
    assert!(problems.iter().any(|p| p.contains("/opt/bin/pg-spawnd")));
    assert!(problems.iter().any(|p| p.contains("/opt/caf\u{e9}/bytes")));
    let declared = Manifest::parse(
        "[[unit]]\nname='u'\nfiles=['u.service']\nhosts=['keeper']\nfirst_run='c'\n\
         [[binary]]\npackage='p'\nbin='pg-spawnd'\nhosts=['keeper']\nfirst_run='c'\n\
         install_paths=[{path='/opt/bin/pg-spawnd',hosts=['keeper']}]\n",
    )
    .unwrap();
    let only: BTreeSet<_> = found
        .iter()
        .filter(|c| c.bin == "pg-spawnd")
        .cloned()
        .collect();
    assert_eq!(declared.consumer_problems(&only), Vec::<String>::new());
}

#[test]
fn an_env_file_path_with_an_interior_tab_can_be_declared() {
    let (dir, shipped) = units("EnvironmentFile=-/srv/q\tdir/x.env\n");
    let env = super::scan::unit_env_files(dir.path(), &shipped).unwrap();
    assert_eq!(
        env,
        BTreeSet::from([(
            "systemd/u.service".to_owned(),
            "/srv/q\tdir/x.env".to_owned()
        )])
    );
    let head = "[[unit]]\nname='u'\nfiles=['u.service']\nhosts=['keeper']\nfirst_run='c'\n";
    let undeclared = Manifest::parse(head).unwrap().env_problems(&env);
    assert_eq!(undeclared.len(), 1, "{undeclared:?}");
    let declared = Manifest::parse(&format!("{head}env_files=['/srv/q\tdir/x.env']\n")).unwrap();
    assert_eq!(declared.env_problems(&env), Vec::<String>::new());
}
