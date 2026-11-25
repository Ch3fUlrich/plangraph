use super::tests::{missing, put, units};
use super::*;
use std::fs;

fn shadow_host(home: &Path, env: &str, config: Option<&str>) {
    put(home, ".local/bin/plangraph", "", 0o755);
    put(
        home,
        ".config/plangraph/state-shadow/routing.env",
        env,
        0o600,
    );
    if let Some(config) = config {
        put(
            home,
            ".config/plangraph/state-shadow/routing.json",
            config,
            0o600,
        );
    }
}

#[test]
fn the_checked_in_state_shadow_unit_needs_its_token_config_and_read_paths() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../deploy/systemd");
    let instance = format!("plangraph-state-shadow{}routing.service", '@');
    let needs = plan(&instance, &root, None).unwrap();
    let home = tempfile::tempdir().unwrap();
    let h = home.path().display();
    // Nothing configured: the unit file names its env file, and the program's own settings are required too.
    shadow_host(home.path(), "OTHER=1\n", None);
    let found = missing(&needs, home.path());
    for want in [
        "env PG_TOKEN_FILE",
        "env PG_URL",
        "file ~/.config/plangraph/state-shadow/routing.json",
    ] {
        assert!(found.contains(&want.to_owned()), "{found:?}");
    }
    // Everything named, but the token, the transcript and the data repository are absent.
    let env = format!("PG_URL=http://k\nPG_TOKEN_FILE={h}/token\n");
    let config = format!(
        "{{\"role\":\"routing\",\"principal\":\"p\",\"directory\":\"{h}/state\",\"inbox\":\"{h}/inbox\",\
         \"transcript\":\"{h}/session.jsonl\",\"session_id\":\"session\",\"data_repo\":\"{h}/data\",\
         \"decision_base\":\"{}\",\"github_repo\":\"example/demo\",\"github_since\":\"2026-01-01T00:00:00Z\",\
         \"quota\":\"{h}/quota.json\"}}",
        "a".repeat(40)
    );
    shadow_host(home.path(), &env, Some(&config));
    assert_eq!(
        missing(&needs, home.path()),
        ["path ~/data", "path ~/session.jsonl", "path ~/token"]
    );
    put(home.path(), "token", "x", 0o600);
    put(home.path(), "session.jsonl", "", 0o600);
    fs::create_dir_all(home.path().join("data")).unwrap();
    assert!(missing(&needs, home.path()).is_empty());
    // A configuration `state wait` would refuse (here: only the two read paths) is a missing file.
    let partial = format!("{{\"transcript\": \"{h}/session.jsonl\", \"data_repo\": \"{h}/data\"}}");
    put(
        home.path(),
        ".config/plangraph/state-shadow/routing.json",
        &partial,
        0o600,
    );
    assert_eq!(
        missing(&needs, home.path()),
        ["file ~/.config/plangraph/state-shadow/routing.json"]
    );
    put(
        home.path(),
        ".config/plangraph/state-shadow/routing.json",
        &config,
        0o600,
    );
    // A configuration that is not JSON is a missing file, and PLANGRAPH_STATE_CONFIG moves it.
    put(
        home.path(),
        ".config/plangraph/state-shadow/routing.json",
        "not json",
        0o600,
    );
    assert_eq!(
        missing(&needs, home.path()),
        ["file ~/.config/plangraph/state-shadow/routing.json"]
    );
    let moved = format!("{env}PLANGRAPH_STATE_CONFIG={h}/other.json\n");
    shadow_host(home.path(), &moved, None);
    put(home.path(), "other.json", &config, 0o600);
    assert!(missing(&needs, home.path()).is_empty());
}

#[test]
fn relative_paths_of_the_state_shadow_configuration_start_at_the_working_directory_of_the_service()
{
    let unit = "[Service]\nWorkingDirectory=%h/work\nEnvironmentFile=%h/.config/p/e.env\nExecStart=%h/.local/bin/plangraph state wait --role routing\n";
    let dir = units(&[("sh.service", unit)]);
    let home = tempfile::tempdir().unwrap();
    let h = home.path().display();
    put(home.path(), ".local/bin/plangraph", "", 0o755);
    put(
        home.path(),
        ".config/p/e.env",
        &format!("PG_URL=http://k\nPG_TOKEN_FILE={h}/token\nPLANGRAPH_STATE_CONFIG={h}/c.json\n"),
        0o600,
    );
    put(home.path(), "token", "x", 0o600);
    let config = format!(
        "{{\"role\":\"routing\",\"principal\":\"p\",\"directory\":\"{h}/state\",\"inbox\":\"{h}/inbox\",\
         \"transcript\":\"rel/session.jsonl\",\"session_id\":\"session\",\"data_repo\":\"rel/data\",\
         \"decision_base\":\"{}\",\"github_repo\":\"example/demo\",\"github_since\":\"2026-01-01T00:00:00Z\",\
         \"quota\":\"{h}/quota.json\"}}",
        "a".repeat(40)
    );
    put(home.path(), "c.json", &config, 0o600);
    fs::create_dir_all(home.path().join("work")).unwrap();
    let needs = plan("sh.service", dir.path(), None).unwrap();
    // Files that exist in the home directory, not in the service's working directory, do not count.
    put(home.path(), "rel/session.jsonl", "", 0o600);
    fs::create_dir_all(home.path().join("rel/data")).unwrap();
    assert_eq!(
        missing(&needs, home.path()),
        ["path ~/work/rel/data", "path ~/work/rel/session.jsonl"]
    );
    put(home.path(), "work/rel/session.jsonl", "", 0o600);
    fs::create_dir_all(home.path().join("work/rel/data")).unwrap();
    assert!(missing(&needs, home.path()).is_empty());
}

#[test]
fn the_service_directory_decides_a_relative_config_and_is_per_instance_for_a_template() {
    // A relative PLANGRAPH_STATE_CONFIG and the paths in it start at the instance's WorkingDirectory.
    let unit = "[Service]\nWorkingDirectory=%h/work/%i\nEnvironmentFile=%h/.config/p/%i.env\nExecStart=%h/.local/bin/plangraph state wait --role %i\n";
    let dir = units(&[("sh@.service", unit)]);
    let home = tempfile::tempdir().unwrap();
    let h = home.path().display();
    put(home.path(), ".local/bin/plangraph", "", 0o755);
    put(home.path(), "token", "x", 0o600);
    put(
        home.path(),
        ".config/p/routing.env",
        &format!("PG_URL=http://k\nPG_TOKEN_FILE={h}/token\nPLANGRAPH_STATE_CONFIG=conf/c.json\n"),
        0o600,
    );
    let config = format!(
        "{{\"role\":\"routing\",\"principal\":\"p\",\"directory\":\"{h}/state\",\"inbox\":\"{h}/inbox\",\
         \"transcript\":\"t/session.jsonl\",\"session_id\":\"session\",\"data_repo\":\"data\",\
         \"decision_base\":\"{}\",\"github_repo\":\"example/demo\",\"github_since\":\"2026-01-01T00:00:00Z\",\
         \"quota\":\"{h}/quota.json\"}}",
        "a".repeat(40)
    );
    let needs = plan("sh@.service", dir.path(), None).unwrap();
    // The configuration exists at the caller-relative place (the home directory), not in the service's one.
    put(home.path(), "conf/c.json", &config, 0o600);
    assert_eq!(
        missing(&needs, home.path()),
        ["file ~/work/routing/conf/c.json"]
    );
    put(home.path(), "work/routing/conf/c.json", &config, 0o600);
    assert_eq!(
        missing(&needs, home.path()),
        [
            "path ~/work/routing/data",
            "path ~/work/routing/t/session.jsonl"
        ]
    );
    put(home.path(), "work/routing/t/session.jsonl", "", 0o600);
    fs::create_dir_all(home.path().join("work/routing/data")).unwrap();
    assert!(missing(&needs, home.path()).is_empty());
    // A directory the dry run cannot expand (here a specifier it does not know) leaves the config unverified.
    let odd = unit.replace("%h/work/%i", "%t/work");
    let dir = units(&[("sh@.service", &odd)]);
    let needs = plan("sh@.service", dir.path(), None).unwrap();
    assert_eq!(missing(&needs, home.path()), ["file conf/c.json"]);
}

#[test]
fn the_disk_hygiene_unit_reports_relative_paths_that_are_missing() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../deploy/systemd");
    let needs = plan("pg-disk-hygiene.service", &root, None).unwrap();
    let home = tempfile::tempdir().unwrap();
    put(home.path(), ".local/bin/pg-disk-hygiene", "", 0o755);
    put(
        home.path(),
        ".config/plangraph/disk-hygiene-service.env",
        "PG_REPO=repo\nPG_WORKTREES=work/trees\nPG_PINS=pins.json\n",
        0o600,
    );
    // The service starts in the home directory: that is where the relative paths are looked for.
    assert_eq!(
        missing(&needs, home.path()),
        ["path ~/pins.json", "path ~/repo", "path ~/work/trees"]
    );
    fs::create_dir_all(home.path().join("repo")).unwrap();
    fs::create_dir_all(home.path().join("work/trees")).unwrap();
    put(home.path(), "pins.json", "{}", 0o600);
    assert!(missing(&needs, home.path()).is_empty());
    // A bare word or a URL is a name, not a path.
    put(
        home.path(),
        ".config/plangraph/disk-hygiene-service.env",
        "PG_REPO=repo\nPG_WORKTREES=work/trees\nPG_PINS=pins.json\nOTHER=plangraph\nURL=http://host\n",
        0o600,
    );
    assert!(missing(&needs, home.path()).is_empty());
}

#[test]
fn an_executable_with_a_specifier_is_resolved_per_instance_or_missing() {
    let unit = "[Service]\nExecStart=%h/missing/%i --x\n";
    let dir = units(&[("t@.service", unit)]);
    let home = tempfile::tempdir().unwrap();
    let check = |family: &str| missing(&plan(family, dir.path(), None).unwrap(), home.path());
    let instance = format!("t{}one.service", '@');
    assert_eq!(check(&instance), ["binary ~/missing/one"]);
    // The template alone names no instance: its executable cannot be resolved, and that is not a pass.
    assert_eq!(check("t@.service"), ["binary ~/missing/%i"]);
    put(home.path(), "missing/one", "", 0o755);
    assert!(check(&instance).is_empty());
}
