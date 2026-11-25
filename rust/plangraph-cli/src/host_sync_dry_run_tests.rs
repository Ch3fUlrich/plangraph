use super::*;
use std::fs;

pub(super) fn units(files: &[(&str, &str)]) -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    for (name, text) in files {
        fs::write(dir.path().join(name), text).unwrap();
    }
    dir
}

pub(super) fn missing(needs: &Needs, home: &Path) -> Vec<String> {
    evaluate(needs, home, &|_| None)
        .into_iter()
        .map(|(k, n)| format!("{k} {n}"))
        .collect()
}

pub(super) fn put(home: &Path, rel: &str, text: &str, mode: u32) {
    use std::os::unix::fs::PermissionsExt;
    let path = home.join(rel);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    if mode & 0o111 != 0 {
        pg_worker_core::executable_test_support::write_executable(&path, text);
    } else {
        fs::write(&path, text).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(mode)).unwrap();
    }
}

const SERVICE: &str = "[Service]\nEnvironmentFile=%h/.config/p/a.env\nEnvironmentFile=-%h/.config/p/opt.env\nExecStart=%h/.local/bin/tool --repo ${PG_REPO} --pins $PG_PINS\n";

#[test]
fn a_host_missing_an_env_key_reports_exactly_that_key() {
    let dir = units(&[("a.service", SERVICE)]);
    let home = tempfile::tempdir().unwrap();
    put(home.path(), ".local/bin/tool", "", 0o755);
    fs::create_dir_all(home.path().join("repo")).unwrap();
    let repo = format!("PG_REPO=\"{}/repo\"\n", home.path().display());
    put(home.path(), ".config/p/a.env", &repo, 0o600);
    let needs = plan("a.service", dir.path(), None).unwrap();
    assert_eq!(missing(&needs, home.path()), ["env PG_PINS"]);
}

#[test]
fn a_complete_host_misses_nothing() {
    let dir = units(&[("a.service", SERVICE)]);
    let home = tempfile::tempdir().unwrap();
    put(home.path(), ".local/bin/tool", "", 0o755);
    let root = home.path().display();
    fs::create_dir_all(home.path().join("repo")).unwrap();
    put(home.path(), "repo/pins.json", "{}", 0o600);
    let text = format!("PG_REPO={root}/repo\n# note\nexport PG_PINS='{root}/repo/pins.json'\n");
    put(home.path(), ".config/p/a.env", &text, 0o600);
    let needs = plan("a.service", dir.path(), None).unwrap();
    assert!(missing(&needs, home.path()).is_empty());
}

#[test]
fn an_empty_host_lists_binary_required_env_file_and_not_the_optional_one() {
    let dir = units(&[("a.service", SERVICE)]);
    let home = tempfile::tempdir().unwrap();
    let needs = plan("a.service", dir.path(), None).unwrap();
    let found = missing(&needs, home.path());
    assert!(
        found.contains(&"binary ~/.local/bin/tool".to_owned()),
        "{found:?}"
    );
    assert!(
        found.contains(&"file ~/.config/p/a.env".to_owned()),
        "{found:?}"
    );
    assert!(found.contains(&"env PG_REPO".to_owned()), "{found:?}");
    assert!(!found.iter().any(|f| f.contains("opt.env")), "{found:?}");
}

#[test]
fn an_env_value_naming_an_absent_path_is_a_missing_path() {
    let dir = units(&[("a.service", SERVICE)]);
    let home = tempfile::tempdir().unwrap();
    put(home.path(), ".local/bin/tool", "", 0o755);
    put(
        home.path(),
        ".config/p/a.env",
        "PG_REPO=%h/gone\nPG_PINS=\n",
        0o600,
    );
    let needs = plan("a.service", dir.path(), None).unwrap();
    assert_eq!(missing(&needs, home.path()), ["env PG_PINS", "path ~/gone"]);
}

#[test]
fn a_template_instance_reads_its_own_env_file() {
    let unit = "[Service]\nEnvironmentFile=%h/.config/p/s/%i.env\nExecStart=%h/.local/bin/tool --role %i\n";
    let dir = units(&[("s@.service", unit)]);
    let home = tempfile::tempdir().unwrap();
    put(home.path(), ".local/bin/tool", "", 0o755);
    let instance = format!("s{}routing.service", '@');
    let needs = plan(&instance, dir.path(), None).unwrap();
    assert_eq!(
        missing(&needs, home.path()),
        ["file ~/.config/p/s/routing.env"]
    );
    put(home.path(), ".config/p/s/routing.env", "A=1\n", 0o600);
    assert!(missing(&needs, home.path()).is_empty());
    // The template itself has no instance: only the directory of the file can be checked.
    let needs = plan("s@.service", dir.path(), None).unwrap();
    assert!(missing(&needs, home.path()).is_empty());
    fs::remove_dir_all(home.path().join(".config/p/s")).unwrap();
    assert_eq!(missing(&needs, home.path()), ["path ~/.config/p/s"]);
}

#[test]
fn a_timer_checks_the_service_it_starts_and_a_missing_source_is_a_missing_file() {
    let dir = units(&[("t.timer", "[Timer]\nOnCalendar=daily\n")]);
    let home = tempfile::tempdir().unwrap();
    let needs = plan("t.timer", dir.path(), None).unwrap();
    assert_eq!(
        missing(&needs, home.path()),
        ["file deploy/systemd/t.service"]
    );
    let dir = units(&[
        ("t.timer", "[Timer]\nOnCalendar=daily\n"),
        ("t.service", "[Service]\nExecStart=%h/bin/x\n"),
    ]);
    let needs = plan("t.timer", dir.path(), None).unwrap();
    assert_eq!(missing(&needs, home.path()), ["binary ~/bin/x"]);
}

#[test]
fn a_built_binary_needs_its_artifact_and_the_install_directory() {
    let home = tempfile::tempdir().unwrap();
    let repo = tempfile::tempdir().unwrap();
    let needs = plan("bin:tool", Path::new("none"), repo.path().to_str()).unwrap();
    let found = missing(&needs, home.path());
    let artifact = format!("binary {}/rust/target/release/tool", repo.path().display());
    assert_eq!(found, [artifact, "path ~/.local/bin".to_owned()]);
    put(repo.path(), "rust/target/release/tool", "", 0o755);
    fs::create_dir_all(home.path().join(".local/bin")).unwrap();
    assert!(missing(&needs, home.path()).is_empty());
    let none = plan("bin:tool", Path::new("none"), None).unwrap();
    assert!(missing(&none, home.path()).contains(&"env CARGO_TARGET_DIR".to_owned()));
}

#[test]
fn bare_family_names_find_the_units_of_the_repository() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../deploy/systemd");
    let needs = plan("disk-hygiene", &root, None).unwrap();
    let keys: Vec<_> = needs.keys.iter().map(String::as_str).collect();
    assert_eq!(keys, ["PG_PINS", "PG_REPO", "PG_WORKTREES"]);
    let shadow = plan("state-shadow", &root, None).unwrap();
    assert!(shadow
        .env_files
        .iter()
        .any(|f| f.path.contains("state-shadow/")));
    let nothing = plan("no-such-family", &root, None).unwrap();
    assert_eq!(
        nothing.absent.into_iter().collect::<Vec<_>>(),
        [(
            "file".to_owned(),
            "deploy/systemd/no-such-family.service".to_owned()
        )]
    );
}

#[test]
fn hostile_family_names_are_refused() {
    for bad in [
        "",
        "-x",
        "../a.service",
        "a b.service",
        "a;b.service",
        "bin:-x",
    ] {
        assert!(plan(bad, Path::new("none"), None).is_err(), "{bad}");
    }
}

#[test]
fn printed_names_have_no_spaces_and_hide_the_home() {
    let home = Path::new("/h/me");
    assert_eq!(shown(Path::new("/h/me/a b/c"), home), "~/a%20b/c");
    assert_eq!(shown(Path::new("/etc/x"), home), "/etc/x");
}

#[test]
fn shell_variables_inside_a_quoted_script_are_not_environment_keys() {
    // systemd substitutes `${NAME}` inside a word and `$NAME` only as a whole word: the locals of a
    // `bash -c` script are the shell's, not the unit's.
    let unit = "[Service]\nExecStart=/bin/bash -c 'S=$(date) && echo \"$S\" \"${PG_OUT:?}\" ${PG_REAL} $$HOME'\nExecStart=/bin/tool $PG_WHOLE \"$PG_QUOTED\" a$NOT_WHOLE --x=${PG_IN_WORD}\n";
    let dir = units(&[("q.service", unit)]);
    let needs = plan("q.service", dir.path(), None).unwrap();
    let keys: Vec<_> = needs.keys.iter().map(String::as_str).collect();
    assert_eq!(keys, ["PG_IN_WORD", "PG_QUOTED", "PG_REAL", "PG_WHOLE"]);
    // The unit of the repository that holds such a script needs only its own keys.
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../deploy/systemd");
    let render = plan("plangraph-render.service", &root, None).unwrap();
    assert!(
        render
            .keys
            .iter()
            .all(|k| k.starts_with("PLANGRAPH_") && !["S", "R", "T"].contains(&k.as_str())),
        "{:?}",
        render.keys
    );
    assert!(
        !render.keys.contains("PLANGRAPH_BUILD_SHA"),
        "{:?}",
        render.keys
    );
}

const TEMPLATE: &str = "[Service]\nEnvironmentFile=%h/.config/p/s/%i.env\nExecStart=%h/.local/bin/tool --peer ${PEER}\n";

#[test]
fn a_template_family_checks_the_keys_of_every_instance_file_on_the_host() {
    let dir = units(&[("s@.service", TEMPLATE)]);
    let home = tempfile::tempdir().unwrap();
    put(home.path(), ".local/bin/tool", "", 0o755);
    let needs = plan("s@.service", dir.path(), None).unwrap();
    // No instance file: the host cannot run any instance.
    fs::create_dir_all(home.path().join(".config/p/s")).unwrap();
    assert_eq!(
        missing(&needs, home.path()),
        ["env PEER", "file ~/.config/p/s/%i.env"]
    );
    put(home.path(), ".config/p/s/a.env", "PEER=x\n", 0o600);
    put(home.path(), ".config/p/s/b.env", "OTHER=1\n", 0o600);
    put(home.path(), ".config/p/s/notes.txt", "", 0o600);
    assert_eq!(missing(&needs, home.path()), ["env PEER"]);
    put(home.path(), ".config/p/s/b.env", "PEER=y\n", 0o600);
    assert!(missing(&needs, home.path()).is_empty());
}

#[test]
fn an_env_file_with_a_specifier_the_dry_run_cannot_expand_is_not_a_pass() {
    let unit = "[Service]\nEnvironmentFile=%t/p/a.env\nExecStart=/bin/true ${KEY}\n";
    let dir = units(&[("u.service", unit)]);
    let home = tempfile::tempdir().unwrap();
    let needs = plan("u.service", dir.path(), None).unwrap();
    assert_eq!(missing(&needs, home.path()), ["file %t/p/a.env"]);
    // A mandatory file that cannot be expanded is missing even when the unit reads no key; an optional one is not.
    let mandatory = "[Service]\nEnvironmentFile=%t/p/a.env\nExecStart=/bin/true\n";
    let optional = "[Service]\nEnvironmentFile=-%t/p/a.env\nExecStart=/bin/true\n";
    let dir = units(&[("v.service", mandatory), ("w.service", optional)]);
    let check = |name: &str| missing(&plan(name, dir.path(), None).unwrap(), home.path());
    assert_eq!(check("v.service"), ["file %t/p/a.env"]);
    assert!(check("w.service").is_empty());
}

#[test]
fn a_bare_exec_name_is_found_in_the_manager_search_path_or_the_unit_path() {
    let dir = units(&[
        ("a.service", "[Service]\nExecStart=sh -c true\n"),
        ("b.service", "[Service]\nExecStart=no-such-tool-here --x\n"),
        (
            "c.service",
            "[Service]\nEnvironment=PATH=%h/.local/bin\nExecStart=mytool\n",
        ),
        ("d.service", "[Service]\nExecStart=./relative\n"),
    ]);
    let home = tempfile::tempdir().unwrap();
    let check = |name: &str| missing(&plan(name, dir.path(), None).unwrap(), home.path());
    assert!(check("a.service").is_empty());
    assert_eq!(check("b.service"), ["binary no-such-tool-here"]);
    assert_eq!(check("c.service"), ["binary mytool"]);
    put(home.path(), ".local/bin/mytool", "", 0o755);
    assert!(check("c.service").is_empty());
    assert_eq!(check("d.service"), ["binary ./relative"]);
}

#[test]
fn a_plain_state_wait_unit_name_is_not_needed_for_other_units() {
    let dir = units(&[("a.service", "[Service]\nExecStart=/bin/true\n")]);
    let needs = plan("a.service", dir.path(), None).unwrap();
    assert!(needs.programs.is_empty());
}
