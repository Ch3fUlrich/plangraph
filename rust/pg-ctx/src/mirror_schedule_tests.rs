use super::*;
use std::cell::Cell;

fn mirror(root: &Path, count: usize) -> Mirror {
    let repos = (0..count)
        .map(|i| {
            (
                format!("repo{i:02}"),
                "https://example.invalid/repo.git".into(),
            )
        })
        .collect();
    Mirror::from_entries(repos, root).unwrap()
}

#[test]
fn fixed_allowance_survives_large_inventory() {
    let root = tempfile::tempdir().unwrap();
    let mirror = mirror(root.path(), 12);
    mirror.fetch_all_with(
        || Duration::ZERO,
        |_, timeout| {
            assert_eq!(timeout, Duration::from_secs(5));
            Err(Alarm::FetchTimedOut)
        },
    );
}

#[test]
fn unfinished_repositories_rotate_across_new_instances() {
    let root = tempfile::tempdir().unwrap();
    let expected = [
        (0..9).collect::<Vec<_>>(),
        vec![9, 10, 11, 0, 1, 2, 3, 4, 5],
        vec![6, 7, 8, 9, 10, 11, 0, 1, 2],
    ];
    for order in expected {
        let mirror = mirror(root.path(), 12);
        let elapsed = Cell::new(Duration::ZERO);
        let mut attempted = Vec::new();
        let results = mirror.fetch_all_with(
            || elapsed.get(),
            |name, timeout| {
                attempted.push(name.to_string());
                let index: usize = name.strip_prefix("repo").unwrap().parse().unwrap();
                assert_eq!(
                    fs::read_to_string(root.path().join(".fetch-next")).unwrap(),
                    format!("repo{:02}", (index + 1) % 12),
                    "the next tick must advance even if this worker is interrupted"
                );
                elapsed.set(elapsed.get() + timeout);
                Err(Alarm::FetchTimedOut)
            },
        );
        let names: Vec<_> = order.iter().map(|i| format!("repo{i:02}")).collect();
        assert_eq!(attempted, names);
        assert_eq!(results.len(), 12);
        for (name, result) in results {
            assert_eq!(
                result,
                Err(if names.contains(&name) {
                    Alarm::FetchTimedOut
                } else {
                    Alarm::FetchDeferred
                })
            );
        }
    }
}

#[test]
fn fast_inventory_finishes_without_deferring() {
    let root = tempfile::tempdir().unwrap();
    let mirror = mirror(root.path(), 20);
    let results = mirror.fetch_all_with(|| Duration::ZERO, |_, _| Err(Alarm::FetchFailed));
    assert_eq!(results.len(), 20);
    assert!(results.values().all(|r| *r == Err(Alarm::FetchFailed)));
}
