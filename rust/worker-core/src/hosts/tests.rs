use super::*;
use serde_json::{json, Value};

const EXAMPLE: &str = include_str!("../../../../docs/schemas/examples/hosts.example.json");
const SCHEMA: &str = include_str!("../../../../docs/schemas/hosts.schema.json");

fn edited(ptr: &str, val: Value) -> Result<HostRegistry, HostsError> {
    let mut d: Value = serde_json::from_str(EXAMPLE).unwrap();
    if let Some(target) = d.pointer_mut(ptr) {
        *target = val;
    } else {
        let (parent, key) = ptr.rsplit_once('/').unwrap();
        d.pointer_mut(parent).unwrap()[key] = val;
    }
    HostRegistry::from_json_str(&d.to_string())
}

#[test]
fn example_parses_and_validates() {
    let reg = HostRegistry::from_json_str(EXAMPLE).unwrap();
    assert_eq!(reg.self_host().id, "code");
    assert!(reg.self_host().repo("plangraph").is_some());
    let dir = tempfile::tempdir().unwrap();
    let f = dir.path().join("hosts.json");
    std::fs::write(&f, EXAMPLE).unwrap();
    assert!(HostRegistry::load(&f).is_ok());
}

#[test]
fn optional_host_launch_config_roundtrips() {
    for (field, value) in [
        ("ssh_target", json!("remote-user@worker.example.invalid")),
        ("env", json!({"PATH": "/opt/fleet/bin:/usr/bin"})),
    ] {
        let reg = edited(&format!("/hosts/0/{field}"), value.clone()).unwrap();
        assert_eq!(serde_json::to_value(reg).unwrap()["hosts"][0][field], value);
    }
}

#[test]
fn v1_version_and_self_resolve() {
    assert!(matches!(
        edited("/version", json!(2)),
        Err(HostsError::VersionSelf(_))
    ));
    assert!(matches!(
        edited("/self", json!("ghost")),
        Err(HostsError::VersionSelf(_))
    ));
}

#[test]
fn v2_one_case_insensitive_namespace() {
    assert!(matches!(
        edited("/hosts/1/aliases/0", json!("DEVBOX")),
        Err(HostsError::Namespace(_))
    ));
}

#[test]
fn v3_paths_absolute_by_host_os() {
    let posix = edited("/hosts/1/state_dir", json!("/srv/fleet/state"));
    assert!(matches!(posix, Err(HostsError::AbsolutePath(_))));
    let drive = edited("/hosts/0/state_dir", json!(r"C:\fleet\state"));
    assert!(matches!(drive, Err(HostsError::AbsolutePath(_))));
    assert!(abs_for(Os::Linux, "/srv/x") && !abs_for(Os::Linux, r"C:\x"));
    assert!(!abs_for(Os::Linux, r"\\srv\sh\x") && !abs_for(Os::Linux, "x/y"));
    assert!(!abs_for(Os::Windows, "C:"));
    assert!(!abs_for(Os::Windows, r"\server"));
    assert!(abs_for(Os::Windows, r"C:\x") && abs_for(Os::Windows, "C:/x"));
    assert!(abs_for(Os::Windows, r"\\srv\sh\x") && !abs_for(Os::Windows, "/srv/x"));
}

#[test]
fn v4_out_of_tree_after_lexical_normalization() {
    let e = edited(
        "/hosts/0/repos/plangraph/python",
        json!("/srv/fleet/code/worktrees/plangraph/../plangraph/x"),
    );
    assert!(matches!(e, Err(HostsError::OutOfTree(_))));
    let win = edited(
        "/hosts/1/repos/plangraph/python",
        json!("c:/Fleet/Workstation/WorkTrees\\plangraph/Scripts/py.exe"),
    );
    assert!(matches!(win, Err(HostsError::OutOfTree(_))));
}

#[cfg(unix)]
#[test]
fn v4_self_host_canonicalizes_symlinks() {
    let tmp = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(tmp.path().join("real/wt/venv/bin")).unwrap();
    std::fs::write(tmp.path().join("real/wt/venv/bin/python"), b"").unwrap();
    std::os::unix::fs::symlink(tmp.path().join("real/wt"), tmp.path().join("link")).unwrap();
    let mut d: Value = serde_json::from_str(EXAMPLE).unwrap();
    let wt = json!(tmp.path().join("real/wt").to_str().unwrap());
    let py = json!(tmp.path().join("link/venv/bin/python").to_str().unwrap());
    d["hosts"][0]["repos"]["plangraph"]["worktrees_root"] = wt;
    d["hosts"][0]["repos"]["plangraph"]["python"] = py;
    let e = HostRegistry::from_json_str(&d.to_string()).unwrap_err();
    assert!(matches!(e, HostsError::OutOfTree(_)));
}

#[test]
fn v5_slot_only_in_cache_env_values() {
    let e = edited("/hosts/0/state_dir", json!("/srv/fleet/%SLOT%/state"));
    assert!(matches!(e, Err(HostsError::SlotToken(_))));
    assert!(HostRegistry::from_json_str(EXAMPLE).is_ok());
}

#[test]
fn v6_numbers_ramp_roles_and_repo_shape() {
    let runs = edited("/hosts/0/caps/max_runs", json!(0));
    assert!(matches!(runs, Err(HostsError::NumbersShape(_))));
    let writers = edited("/hosts/0/caps/max_writers", json!(11));
    assert!(matches!(writers, Err(HostsError::NumbersShape(_))));
    let ramp = edited(
        "/hosts/0/warmup/ramp_mb",
        json!({"implement": 1, "deploy": 2}),
    );
    assert!(matches!(ramp, Err(HostsError::NumbersShape(_))));
    let imp = edited("/hosts/0/repos/plangraph/import_roots", json!(["../rust"]));
    assert!(matches!(imp, Err(HostsError::NumbersShape(_))));
}

#[test]
fn v6_refuses_bad_repo_id() {
    let repo = json!({
        "root": "/srv/fleet/code/repos/x", "worktrees_root": "/srv/fleet/code/worktrees/x",
        "remote": "placeholder", "import_roots": [], "cache_env": {}
    });
    let e = edited("/hosts/0/repos", json!({"Bad Repo": repo}));
    assert!(matches!(e, Err(HostsError::NumbersShape(_))));
}

#[test]
fn v7_unknown_fields_refused() {
    assert!(matches!(
        edited("/hosts/0/caps/max_run", json!(1)),
        Err(HostsError::UnknownField(_))
    ));
}

#[test]
fn unknown_fields_name_the_host_repo_and_field() {
    let mut d: Value = serde_json::from_str(EXAMPLE).unwrap();
    for ptr in [
        "",
        "/hosts/0",
        "/hosts/0/caps",
        "/hosts/0/warmup",
        "/hosts/0/repos/plangraph",
    ] {
        d.pointer_mut(ptr).unwrap()["typo"] = json!(1);
        let err = HostRegistry::from_json_str(&d.to_string()).unwrap_err();
        assert!(matches!(err, HostsError::UnknownField(_)));
        let message = err.to_string();
        assert!(message.contains("field=typo"));
        if ptr.contains("repos") {
            assert!(message.contains("host=code repo=plangraph"));
        }
        d.pointer_mut(ptr)
            .unwrap()
            .as_object_mut()
            .unwrap()
            .remove("typo");
    }
}

#[test]
fn slot_tokens_and_import_roots_cover_non_path_fields() {
    for (ptr, value) in [
        ("/self", json!("%SLOT%")),
        ("/hosts/0/aliases/0", json!("%SLOT%")),
        ("/hosts/0/warmup/ramp_mb", json!({"%SLOT%": 1})),
        ("/hosts/0/repos/plangraph/remote", json!("%SLOT%")),
        (
            "/hosts/0/repos/plangraph/cache_env",
            json!({"CACHE": "/srv/fleet/prefix%SLOT%"}),
        ),
    ] {
        assert!(matches!(edited(ptr, value), Err(HostsError::SlotToken(_))));
    }
    assert!(matches!(
        edited(
            "/hosts/1/repos/plangraph/import_roots",
            json!(["C:relative"])
        ),
        Err(HostsError::NumbersShape(_))
    ));
}

#[test]
fn normalization_handles_roots_boundaries_and_unc() {
    use super::validate::outside;
    assert!(!outside("/a//b/./c/../", "/a/b/", Os::Linux));
    assert!(outside("/a/b-other", "/a/b", Os::Linux));
    assert!(!outside("/a", "/", Os::Linux));
    assert!(!outside(
        r"\\placeholder\share\tree\..\tree\x",
        "//PLACEHOLDER/share/tree/",
        Os::Windows
    ));
}

#[test]
fn resolve_maps_ids_and_aliases_any_case() {
    let reg = HostRegistry::from_json_str(EXAMPLE).unwrap();
    assert_eq!(reg.resolve("CODE").unwrap().id, "code");
    assert_eq!(reg.resolve("WinBox").unwrap().id, "workstation");
    assert!(reg.resolve("missing").is_none());
}

#[test]
fn schema_fields_required_and_patterns_match_rust() {
    let schema: Value = serde_json::from_str(SCHEMA).unwrap();
    let doc = serde_json::to_value(HostRegistry::from_json_str(EXAMPLE).unwrap()).unwrap();
    let host = &doc["hosts"][0];
    let repo = host["repos"].as_object().unwrap().values().next().unwrap();
    let samples = [
        ("$root", &doc),
        ("hostEntry", host),
        ("caps", &host["caps"]),
        ("warmup", &host["warmup"]),
        ("repoEntry", repo),
    ];
    for (name, sample) in samples {
        let node = if name == "$root" {
            &schema
        } else {
            &schema["$defs"][name]
        };
        let props: std::collections::BTreeSet<&str> = node["properties"]
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect();
        let keys: std::collections::BTreeSet<&str> = sample
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect();
        assert_eq!(props, keys, "schema node {name}");
        let required: std::collections::BTreeSet<&str> = node["required"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect();
        let expected: std::collections::BTreeSet<&str> = keys
            .into_iter()
            .filter(|key| {
                name != "repoEntry" || !["python", "live_dir", "health_checks"].contains(key)
            })
            .filter(|key| name != "hostEntry" || !["class", "ssh_target", "env"].contains(key))
            .collect();
        assert_eq!(required, expected, "required fields for {name}");
    }
    assert_eq!(
        schema["$defs"]["hostEntry"]["properties"]["id"]["pattern"],
        HOST_ID_PATTERN
    );
    assert_eq!(
        schema["$defs"]["hostEntry"]["properties"]["repos"]["propertyNames"]["pattern"],
        REPO_ID_PATTERN
    );
}

#[test]
fn v2_invalid_host_ids_refused() {
    for id in [
        "",
        "Code",
        "1code",
        "bad_id",
        "abcdefghijklmnopqrstuvwxyzabcdefg",
    ] {
        assert!(matches!(
            edited("/hosts/0/id", json!(id)),
            Err(HostsError::Namespace(_))
        ));
    }
}

#[test]
fn duplicate_roles_refused() {
    assert!(matches!(
        edited("/hosts/0/roles", json!(["implement", "implement"])),
        Err(HostsError::DuplicateRole(_))
    ));
}

#[test]
fn v6_seats_warmup_and_session_refused() {
    for (ptr, value) in [
        ("/hosts/0/caps/max_seats", json!(999)),
        ("/hosts/0/warmup/warmup_s", json!(0)),
        ("/hosts/0/session_mb", json!(0)),
    ] {
        assert!(
            matches!(edited(ptr, value), Err(HostsError::NumbersShape(_))),
            "{ptr}"
        );
    }
}

#[cfg(unix)]
#[test]
fn v4_self_host_resolves_missing_leaf_and_worktree_ancestors() {
    let tmp = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(tmp.path().join("wt/sub")).unwrap();
    std::os::unix::fs::symlink(tmp.path().join("wt/sub"), tmp.path().join("link")).unwrap();
    std::os::unix::fs::symlink(tmp.path().join("wt"), tmp.path().join("wt-link")).unwrap();
    for field in ["python", "cache_env"] {
        for missing_root in [false, true] {
            let mut d: Value = serde_json::from_str(EXAMPLE).unwrap();
            let repo = &mut d["hosts"][0]["repos"]["plangraph"];
            let (root, leaf) = if missing_root {
                ("wt-link/new", "wt/new/venv/bin/python")
            } else {
                ("wt", "link/venv/bin/python")
            };
            repo["worktrees_root"] = json!(tmp.path().join(root));
            let path = json!(tmp.path().join(leaf));
            repo[field] = if field == "cache_env" {
                json!({"CACHE": path})
            } else {
                path
            };
            assert!(
                matches!(
                    HostRegistry::from_json_str(&d.to_string()),
                    Err(HostsError::OutOfTree(_))
                ),
                "{field}, missing_root={missing_root}"
            );
        }
    }
}
