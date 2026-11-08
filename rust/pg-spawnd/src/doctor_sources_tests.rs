use super::*;

fn host() -> Value {
    json!({"name":"a", "target":"a", "src":"/src"})
}

#[test]
fn states_and_private_errors() {
    for (code, detail) in [
        (0, "ok"),
        (44, "src missing"),
        (255, "unreachable"),
        (1, "host query failed"),
        (-1, "host query failed"),
    ] {
        assert_eq!(
            check(&host(), &|_, _| Ok(Some(code))),
            row("ps_host_a", detail)
        );
    }
    assert_eq!(
        check(&host(), &|_, _| Ok(None)),
        row("ps_host_a", "host query failed")
    );
    assert_eq!(
        check(&host(), &|_, _| Err(io::Error::other("PRIVATE"))),
        row("ps_host_a", "unreachable")
    );
    for h in [
        json!({"name":"a"}),
        json!({"name":"a", "src":3}),
        json!({"name":"a", "win_src":"bad"}),
        json!({"name":"a", "src":"/src", "wsl_distro":3}),
    ] {
        assert_eq!(
            check(&h, &|_, _| panic!()),
            row("ps_host_a", "invalid configuration")
        );
    }
}

#[test]
fn config_and_filtering() {
    let run = |_: &str, _: &str| Ok(Some(0));
    for data in ["bad", "{}", "{\"hosts\":{}}"] {
        assert_eq!(
            source_checks(Ok(data.into()), &run),
            vec![row("ps_hosts", "invalid configuration")]
        );
    }
    assert_eq!(
        source_checks(Err(io::Error::other("PRIVATE")), &run),
        vec![row("ps_hosts", "invalid configuration")]
    );
    assert!(source_checks(Err(io::ErrorKind::NotFound.into()), &run).is_empty());
    for data in [
        json!({"hosts":[]}),
        json!({"hosts":[null,3,"host",[],{}, {"name":"a","target":"a"}]}),
    ] {
        assert!(source_checks(Ok(data.to_string()), &run).is_empty());
    }
    let data = json!({"hosts":[host(), {"name":"b","target":"b","win_src":"C:/src"}]});
    assert_eq!(
        source_checks(Ok(data.to_string()), &run),
        vec![row("ps_host_a", "ok"), row("ps_host_b", "ok")]
    );
}

fn assert_filtered(h: Value) {
    assert!(!valid(&h), "unexpectedly valid: {h}");
    assert!(
        source_checks(Ok(json!({"hosts":[h]}).to_string()), &|_, _| panic!(
            "filtered host must not run SSH"
        ))
        .is_empty()
    );
}

#[test]
fn invalid_source_combinations_are_filtered() {
    for h in [
        json!({"name":"a", "target":"a", "src":"/src", "win_src":"C:/src"}),
        json!({"name":"a", "target":"a", "win_src":"C:/src", "wsl_distro":"Ubuntu"}),
        json!({"name":"a", "target":"a", "src":"~/src", "wsl_distro":"Ubuntu"}),
        json!({"name":"a", "target":"a", "wsl_distro":"Ubuntu"}),
        json!({"name":"invalid-config", "invalid":true}),
    ] {
        assert_filtered(h);
    }
}

#[test]
fn invalid_command_is_filtered() {
    assert_filtered(json!({"name":"a", "target":"a", "win_src":"bad"}));
}

#[test]
fn configured_host_regexes_match_legacy_filter() {
    // Golden acceptance/rejection cases: no interpreter, filesystem, or site-packages.
    // Keep the Python batching deadline tied to the actual Rust probe limits.
    let remote = include_str!("../../../src/plangraph/spawner/remote_ps.py");
    assert!(remote.contains(&format!("MAX_WORKERS = {MAX_WORKERS}\n")));
    assert!(remote.contains(&format!("TIMEOUT = {}\n", TIMEOUT.as_secs())));
    let cases = [
        (
            "name",
            vec![json!("a"), json!("A0_.-"), json!("a".repeat(63))],
            vec![
                json!(3),
                json!(""),
                json!("-a"),
                json!("a".repeat(64)),
                json!("a\n"),
            ],
        ),
        (
            "target",
            vec![json!("user@host:22"), json!("_a.-")],
            vec![
                json!(3),
                json!(""),
                json!("-host"),
                json!("host space"),
                json!("host\n"),
            ],
        ),
        (
            "src",
            vec![json!("/"), json!("~/"), json!("/source space/_.-09")],
            vec![
                json!(3),
                json!(""),
                json!("relative"),
                json!("/src;bad"),
                json!("/src\n"),
            ],
        ),
        (
            "win_src",
            vec![json!("C:/"), json!(r"z:\source space\_.-09")],
            vec![
                json!(3),
                json!("bad"),
                json!("C:relative"),
                json!("C:/src;bad"),
                json!("C:/src\n"),
            ],
        ),
        (
            "wsl_distro",
            vec![json!("Ubuntu-24.04"), json!("_"), json!(".")],
            vec![
                json!(3),
                json!(""),
                json!("-Ubuntu"),
                json!("Ubuntu space"),
                json!("Ubuntu\n"),
            ],
        ),
    ];
    for (key, accepted, rejected) in cases {
        for (values, expected) in [(accepted, true), (rejected, false)] {
            for value in values {
                let mut h = host();
                if key == "win_src" {
                    h.as_object_mut().unwrap().remove("src");
                }
                h[key] = value;
                if expected {
                    assert!(valid(&h), "unexpectedly invalid: {h}");
                } else {
                    assert_filtered(h.clone());
                }
            }
        }
    }
}

#[test]
fn parallel_bounded_and_ordered() {
    let barrier = std::sync::Barrier::new(MAX_WORKERS);
    let active = std::sync::atomic::AtomicUsize::new(0);
    let data = json!({"hosts": (0..32).map(|i| json!({"name":format!("h{i}"),"target":"a","src":"/src"})).collect::<Vec<_>>()});
    let rows = source_checks(Ok(data.to_string()), &|_, _| {
        assert!(active.fetch_add(1, std::sync::atomic::Ordering::SeqCst) < MAX_WORKERS);
        barrier.wait();
        active.fetch_sub(1, std::sync::atomic::Ordering::SeqCst);
        barrier.wait();
        Ok(None)
    });
    for (i, r) in rows.iter().enumerate() {
        assert_eq!(*r, row(&format!("ps_host_h{i}"), "host query failed"));
    }
}

#[test]
fn commands() {
    assert_eq!(command(&host()).unwrap(), "test -d '/src' || exit 44");
    assert_eq!(
        command(&json!({"src":"~/source space"})).unwrap(),
        "test -d \"$HOME\"/'source space' || exit 44"
    );
    assert_eq!(
        command(&json!({"win_src":"C:/src"})).unwrap(),
        "if not exist \"C:/src\\\" exit /b 44"
    );
    assert!(command(&json!({"src":"/src", "wsl_distro":"Ubuntu"}))
        .unwrap()
        .starts_with("wsl -d Ubuntu -- sh -c '"));
}
