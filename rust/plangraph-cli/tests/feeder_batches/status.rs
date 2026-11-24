//! One status file per launch: no two runs, retries or look-alike keys share one.
use super::*;

/// The status file a batch brief names, parsed from its `PLANGRAPH-BATCH-INPUT-V1` payload.
fn brief_status_file(brief: &str) -> String {
    let (_, payload) = brief.split_once('\n').unwrap();
    let payload: Value = serde_json::from_str(payload).unwrap();
    payload["run"]["status_file"].as_str().unwrap().to_owned()
}
fn launch_paths(registry: &Registry) -> Vec<String> {
    registry
        .launches
        .iter()
        .map(|(card, brief)| {
            // The card names the file the native writer host records as writer_status_path
            // and the brief quotes that same file.
            let path = card["writer_status_path"].as_str().unwrap().to_owned();
            // A single-item brief is plain text and names no status file.
            if brief.starts_with("PLANGRAPH-BATCH-INPUT-V1") {
                assert_eq!(brief_status_file(brief), path);
            }
            path
        })
        .collect()
}

#[test]
fn overlapping_batch_runs_write_their_own_status_file_and_never_mix_done_lines() {
    let reviews = std::env::temp_dir().join(format!("feeder-s2-{}", std::process::id()));
    std::fs::create_dir_all(&reviews).unwrap();
    let reviews_dir = reviews.to_str().unwrap().to_owned();
    let items = vec![
        item("ALPHA-1", 9, &["src/a.rs"]),
        item("ALPHA-2", 8, &["src/a.rs"]),
        item("BETA-1", 7, &["src/b.rs"]),
        item("BETA-2", 6, &["src/b.rs"]),
    ];
    let registry: Arc<Mutex<Registry>> = Arc::default();
    let meet = Arc::new(Barrier::new(2));
    // Each host sees one family, so each launches a two-item batch; both hold their first
    // claim until the other has claimed: the runs overlap.
    std::thread::scope(|scope| {
        for (name, own) in [("dell", &items[..2]), ("coding", &items[2..])] {
            let mut host = Host::new(&registry, &items);
            host.meet = Some(meet.clone());
            let (doc, dir) = (ready(own.to_vec()), &reviews_dir);
            scope.spawn(move || drive_in(dir, &doc, &mut host, name, 2, false));
        }
    });
    let registry = registry.lock().unwrap();
    assert_eq!(registry.launches.len(), 2, "{:?}", registry.started);
    let paths = launch_paths(&registry);
    assert_ne!(paths[0], paths[1], "two runs must not share a status file");
    for (card, path) in registry.launches.iter().map(|l| &l.0).zip(&paths) {
        assert_eq!(std::path::Path::new(path).parent().unwrap(), reviews);
        // Each writer appends only its own items to its own file.
        let own: Vec<&str> = card["item_keys"]
            .as_array()
            .unwrap()
            .iter()
            .map(|k| k.as_str().unwrap())
            .collect();
        let lines: String = own.iter().map(|k| format!("DONE {k} abc\n")).collect();
        std::fs::write(path, lines).unwrap();
    }
    for (card, path) in registry.launches.iter().map(|l| &l.0).zip(&paths) {
        let text = std::fs::read_to_string(path).unwrap();
        let own = card["item_keys"][0].as_str().unwrap();
        let rival = if own.contains("ALPHA") {
            "BETA"
        } else {
            "ALPHA"
        };
        assert!(
            text.contains(own) && !text.contains(rival),
            "{path}: {text}"
        );
    }
    std::fs::remove_dir_all(&reviews).unwrap();
}

#[test]
fn keys_that_normalise_alike_and_retries_never_share_a_status_file() {
    let items = vec![
        item("FOO.BAR-1", 9, &["src/a.rs"]),
        item("FOO_BAR-1", 8, &["src/b.rs"]),
    ];
    // Same slug, different raw keys.
    let paths: Vec<String> = items
        .iter()
        .map(|one| {
            let registry: Arc<Mutex<Registry>> = Arc::default();
            let mut host = Host::new(&registry, &items);
            drive(&ready(vec![one.clone()]), &mut host, "dell", 1, false);
            let paths = launch_paths(&registry.lock().unwrap());
            paths[0].clone()
        })
        .collect();
    assert_ne!(paths[0], paths[1]);
    // The same item launched twice (the first start failed and was released) gets a new file.
    let retry = vec![item("ALPHA-1", 9, &["src/a.rs"])];
    let registry: Arc<Mutex<Registry>> = Arc::default();
    let mut host = Host::new(&registry, &retry);
    host.fail_start = true;
    drive(&ready(retry.clone()), &mut host, "dell", 1, false);
    host.fail_start = false;
    drive(&ready(retry), &mut host, "dell", 1, false);
    let registry = registry.lock().unwrap();
    assert_eq!(registry.launches.len(), 1);
    assert!(registry.epochs.get("plangraph:ALPHA-1").unwrap() > &100);
    let path = launch_paths(&registry)[0].clone();
    assert!(path.ends_with("-e101.txt"), "{path}");
}

#[test]
fn a_single_item_run_also_gets_its_own_status_file() {
    let items = vec![item("ALPHA-1", 9, &["src/a.rs"])];
    let registry: Arc<Mutex<Registry>> = Arc::default();
    let mut host = Host::new(&registry, &items);
    drive(&ready(items), &mut host, "dell", 1, false);
    let path = launch_paths(&registry.lock().unwrap())[0].clone();
    assert!(
        path.starts_with("/reviews/status-alpha-1-") && path.ends_with("-e100.txt"),
        "{path}"
    );
}
