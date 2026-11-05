use super::*;
#[test]
fn placement_rule_docs_use_production_only_budget() {
    let docs = include_str!("../../../docs/placement-rules.md");
    let changelog = include_str!("../../../changelog.d/PLACEMENT-RULES-AUDIT.md");
    for text in [docs, changelog] {
        let text = text.to_lowercase();
        assert!(!text.contains("re-split"), "obsolete split requirement");
        assert!(!text.contains("not merge-ready"), "obsolete size blocker");
        assert!(text.contains("production lines only"));
    }
    assert!(docs.contains("not production durability"));
    assert!(docs.contains("Dependencies before item closure:"));
}
const KEY: &str = "PG_RULE_HOST_WORKER_CAP_COUNT";
#[derive(Default)]
struct Fake {
    snapshot: RuleSnapshot,
    audit: Vec<RuleAudit>,
    failure: Option<StoreError>,
    calls: usize,
}
impl RuleStore for Fake {
    fn read(&self) -> Result<RuleSnapshot, StoreError> {
        self.failure.map_or_else(|| Ok(self.snapshot.clone()), Err)
    }
    fn commit(&mut self, actor: &str, request: &RuleEdit) -> Result<RuleAudit, StoreError> {
        self.calls += 1;
        if request.expected_revision != self.snapshot.revision {
            return Err(StoreError::Conflict);
        }
        if let Some(revision) = request.rollback_of {
            let target = self
                .audit
                .iter()
                .find(|row| row.revision == revision)
                .ok_or(StoreError::Invalid)?;
            if target.edit.scope != request.scope
                || target.edit.key != request.key
                || target.previous != request.value
            {
                return Err(StoreError::Invalid);
            }
        }
        let mut next = self.snapshot.clone();
        let map = match &request.scope {
            RuleScope::Fleet => &mut next.fleet,
            RuleScope::Host(host) => next.hosts.entry(host.clone()).or_default(),
        };
        let previous = map.get(&request.key).cloned();
        if request.value.is_none() && previous.is_none() {
            return Err(StoreError::Invalid);
        }
        if let Some(value) = &request.value {
            map.insert(request.key.clone(), value.clone());
        } else {
            map.remove(&request.key);
        }
        next.revision = next.revision.checked_add(1).ok_or(StoreError::Conflict)?;
        let row = RuleAudit {
            revision: next.revision,
            actor: actor.into(),
            edit: request.clone(),
            previous,
        };
        if let Some(error) = self.failure {
            return Err(error);
        }
        self.snapshot = next;
        self.audit.push(row.clone());
        Ok(row)
    }
}
fn request(revision: u64, value: Option<&str>) -> RuleEdit {
    RuleEdit {
        key: KEY.into(),
        value: value.map(str::to_owned),
        scope: RuleScope::Fleet,
        expected_revision: revision,
        rollback_of: None,
    }
}
#[test]
fn valid_edit_is_atomic_audit() {
    let mut store = Fake::default();
    let row = edit(&mut store, "operator", request(0, Some("7"))).unwrap();
    assert_eq!((row.revision, row.previous), (1, None));
    assert_eq!(store.snapshot.fleet[KEY], "7");
    store.failure = Some(StoreError::Io);
    assert_eq!(
        edit(&mut store, "operator", request(1, Some("8"))),
        Err(StoreError::Io)
    );
    assert_eq!((store.snapshot.revision, store.audit.len()), (1, 1));
    assert_eq!(store.snapshot.fleet[KEY], "7");
}
#[test]
fn unauthorized_actor_refused() {
    let mut store = Fake::default();
    assert_eq!(
        edit(&mut store, " \n", request(0, Some("7"))),
        Err(StoreError::Unauthorized)
    );
    assert_eq!(store.calls, 0);
}
#[test]
fn allowlist_before_store_write() {
    let mut store = Fake::default();
    let mut r = request(0, None);
    r.key = "OTHER".into();
    assert_eq!(edit(&mut store, "operator", r), Err(StoreError::Invalid));
    assert_eq!(store.calls, 0);
}
#[test]
fn invalid_value_preserves_snapshot() {
    let mut store = Fake::default();
    for value in ["-1", "1025", "", "+7"] {
        assert_eq!(
            edit(&mut store, "operator", request(0, Some(value))),
            Err(StoreError::Invalid)
        );
    }
    assert_eq!(store.calls, 0);
    assert_eq!(store.snapshot, RuleSnapshot::default());
}
#[test]
fn stale_revision_conflicts() {
    let mut store = Fake::default();
    let r = request(0, Some("7"));
    edit(&mut store, "operator", r.clone()).unwrap();
    assert_eq!(edit(&mut store, "operator", r), Err(StoreError::Conflict));
    assert_eq!(store.audit.len(), 1);
}
#[test]
fn rollback_appends_forward_revision() {
    let mut store = Fake::default();
    edit(&mut store, "operator", request(0, Some("7"))).unwrap();
    edit(&mut store, "operator", request(1, Some("8"))).unwrap();
    let mut r = request(2, Some("7"));
    r.rollback_of = Some(2);
    let row = edit(&mut store, "operator", r.clone()).unwrap();
    assert_eq!((row.revision, row.previous.as_deref()), (3, Some("8")));
    assert_eq!(store.audit.len(), 3);
    assert_eq!(edit(&mut store, "operator", r), Err(StoreError::Conflict));
}
#[test]
fn rollback_first_insert_deletes_override() {
    let mut store = Fake::default();
    edit(&mut store, "operator", request(0, Some("7"))).unwrap();
    let mut r = request(1, None);
    r.rollback_of = Some(1);
    edit(&mut store, "operator", r).unwrap();
    assert!(store.snapshot.fleet.is_empty());
    assert_eq!(
        edit(&mut store, "operator", request(2, None)),
        Err(StoreError::Invalid)
    );
    assert_eq!(store.audit.len(), 2);
}
#[test]
fn rollback_cannot_cross_scope() {
    let mut store = Fake::default();
    edit(&mut store, "operator", request(0, Some("7"))).unwrap();
    let mut r = request(1, None);
    r.rollback_of = Some(1);
    r.scope = RuleScope::Host("fixture".into());
    assert_eq!(edit(&mut store, "operator", r), Err(StoreError::Invalid));
    assert_eq!(store.audit.len(), 1);
}
fn populated() -> Fake {
    let mut store = Fake::default();
    edit(&mut store, "operator", request(0, Some("7"))).unwrap();
    store
}
#[test]
fn unavailable_uses_lkg() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("cache");
    let mut store = populated();
    let online = load_cached(&store, &path).unwrap();
    store.failure = Some(StoreError::Unavailable);
    assert_eq!(load_cached(&store, &path).unwrap(), online);
}
#[test]
fn current_env_over_cached_keeper() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("cache");
    let mut store = populated();
    load_cached(&store, &path).unwrap();
    store.failure = Some(StoreError::Unavailable);
    let layers = RuleLayers {
        keeper_fleet: load_cached(&store, &path).unwrap().fleet,
        process: [(KEY.into(), "9".into())].into(),
        ..Default::default()
    };
    assert_eq!(resolve(&schema().unwrap(), "", &layers).values[KEY], 9);
}
#[test]
fn corrupt_or_missing_cache_fails_closed() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("cache");
    let store = Fake {
        failure: Some(StoreError::Unavailable),
        ..Default::default()
    };
    assert_eq!(load_cached(&store, &path), Err(StoreError::Unavailable));
    for bytes in [
        b"{".as_slice(),
        b"{}",
        br#"{"version":2,"snapshot":{"revision":0,"fleet":{},"hosts":{}}}"#,
    ] {
        fs::write(&path, bytes).unwrap();
        assert_eq!(load_cached(&store, &path), Err(StoreError::Unavailable));
    }
    fs::write(&path, vec![b' '; MAX_CACHE as usize + 1]).unwrap();
    assert_eq!(load_cached(&store, &path), Err(StoreError::Unavailable));
}
#[test]
fn invalid_keeper_never_masks_as_outage() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("cache");
    let mut store = populated();
    load_cached(&store, &path).unwrap();
    let old = fs::read(&path).unwrap();
    store.snapshot.fleet.insert(KEY.into(), "bad".into());
    assert_eq!(load_cached(&store, &path), Err(StoreError::Invalid));
    for error in [
        StoreError::Unauthorized,
        StoreError::Conflict,
        StoreError::Invalid,
        StoreError::Io,
    ] {
        store.failure = Some(error);
        assert_eq!(load_cached(&store, &path), Err(error));
    }
    assert_eq!(fs::read(&path).unwrap(), old);
}
#[test]
fn cache_replace_failure_preserves_old() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("cache");
    let mut store = populated();
    load_cached(&store, &path).unwrap();
    let old = fs::read(&path).unwrap();
    edit(&mut store, "operator", request(1, Some("8"))).unwrap();
    let temp = sibling(&path, &format!(".{}.tmp", std::process::id()));
    fs::write(&temp, b"owned by another attempt").unwrap();
    assert_eq!(load_cached(&store, &path), Err(StoreError::Io));
    assert_eq!(fs::read(&path).unwrap(), old);
    fs::remove_file(&temp).unwrap();
    let _guard = crate::lockfile::try_lock(&sibling(&path, ".lock"))
        .unwrap()
        .unwrap();
    assert_eq!(load_cached(&store, &path), Err(StoreError::Io));
    assert_eq!(fs::read(&path).unwrap(), old);
}
