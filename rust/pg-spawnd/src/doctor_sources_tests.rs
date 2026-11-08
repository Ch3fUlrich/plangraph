use super::*;

fn host() -> Value { json!({"name":"a", "target":"a", "src":"/src"}) }

#[test]
fn states_and_private_errors() {
    for (code, detail) in [(0,"ok"), (44,"src missing"), (255,"unreachable"), (1,"host query failed"), (-1,"host query failed")] {
        assert_eq!(check(&host(), &|_, _| Ok(Some(code))), row("ps_host_a", detail));
    }
    assert_eq!(check(&host(), &|_, _| Ok(None)), row("ps_host_a", "host query failed"));
    assert_eq!(check(&host(), &|_, _| Err(io::Error::other("PRIVATE"))), row("ps_host_a", "unreachable"));
    for h in [json!({"name":"a"}), json!({"name":"a", "src":3}), json!({"name":"a", "win_src":"bad"}), json!({"name":"a", "src":"/src", "wsl_distro":3})] {
        assert_eq!(check(&h, &|_, _| panic!()), row("ps_host_a", "invalid configuration"));
    }
}

#[test]
fn config_and_filtering() {
    let run = |_: &str, _: &str| Ok(Some(0));
    for data in ["bad", "{}", "{\"hosts\":{}}"] {
        assert_eq!(source_checks(Ok(data.into()), &run), vec![row("ps_hosts", "invalid configuration")]);
    }
    assert_eq!(source_checks(Err(io::Error::other("PRIVATE")), &run), vec![row("ps_hosts", "invalid configuration")]);
    assert!(source_checks(Err(io::ErrorKind::NotFound.into()), &run).is_empty());
    for data in [json!({"hosts":[]}), json!({"hosts":[null,3,"host",[],{}, {"name":"a","target":"a"}]})] {
        assert!(source_checks(Ok(data.to_string()), &run).is_empty());
    }
    let data = json!({"hosts":[host(), {"name":"b","target":"b","win_src":"C:/src"}]});
    assert_eq!(source_checks(Ok(data.to_string()), &run), vec![row("ps_host_a","ok"), row("ps_host_b","ok")]);
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
    for (i, r) in rows.iter().enumerate() { assert_eq!(*r, row(&format!("ps_host_h{i}"), "host query failed")); }
}

#[test]
fn commands() {
    assert_eq!(command(&host()).unwrap(), "test -d '/src' || exit 44");
    assert_eq!(command(&json!({"src":"~/source space"})).unwrap(), "test -d \"$HOME\"/'source space' || exit 44");
    assert_eq!(command(&json!({"win_src":"C:/src"})).unwrap(), "if not exist \"C:/src\\\" exit /b 44");
    assert!(command(&json!({"src":"/src", "wsl_distro":"Ubuntu"})).unwrap().starts_with("wsl -d Ubuntu -- sh -c '"));
}
