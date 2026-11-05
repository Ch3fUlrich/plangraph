//! Unwired store contract: no writes, launches, kills, or lifecycle callers.
//! Watchdog, retirement, router, timer, finalizer, exit and fencing must later
//! consume this same session/run record under their own keys; none is wired here.
//! JSON uses schema_version=1 and externally tagged Unknown/Free/Busy states;
//! every lifecycle field is required. Identity includes pid, start and group.
//! v0 decoding preserves identity/epoch but requires explicit reconciliation.
//! Python consumers read JSON or use a later thin binary: SourceFileLoader
//! cannot import Rust. No binary is needed for this contract-only slice.
//! Paths are caller-supplied relative slots, never derived from opaque IDs.
//! Part 2 owns explicit expected-epoch reconciliation, durable CAS and activation.
//! Known is validated data, not ownership permission; Unknown never authorizes.
//! Read never infers death, expiry or freedom from process observations.
use serde::{Deserialize, Serialize};
use std::{
    io,
    path::{Component, Path, PathBuf},
};

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum State<T> {
    /// Evidence unavailable; never authorizes.
    Unknown,
    /// Explicitly unoccupied.
    Free,
    /// Occupied by the recorded value.
    Busy(T),
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Identity {
    pub pid: u32,
    pub start: String,
    pub group: u32,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Lease {
    pub id: String,
    pub owner: Identity,
    pub expires_at: i64,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Successor {
    /// Evidence unavailable; never authorizes.
    Unknown,
    /// Stable replacement identifier.
    Id(String),
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Phase {
    /// Intent recorded before launch.
    Prepared,
    /// Replacement launched with recorded identity.
    Launched,
    /// Confirmed replacement; retain evidence.
    Confirmed,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Transaction {
    pub id: String,
    pub phase: Phase,
    pub successor: Successor,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Record {
    pub schema_version: u32,
    pub session_id: String,
    pub run_id: String,
    pub epoch: u64,
    pub identity: State<Identity>,
    pub updated_at: i64,
    pub pending: State<Transaction>,
    pub pause: State<Lease>,
    pub finalizer: State<Lease>,
    pub runners: State<Vec<Lease>>,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Refusal {
    Invalid,
    Clock,
    Unresolved,
}
#[derive(Clone, Debug, PartialEq, Eq)]
#[allow(clippy::large_enum_variant)] // One slot per read; keep the public value API unboxed.
pub enum Read {
    Known(Record),
    Unknown {
        session_id: String,
        reason: String,
        alarm: bool,
    },
}
fn valid_id(s: &str) -> bool {
    !s.is_empty()
        && !s.chars().any(|c| c.is_control() || c == '/' || c == '\\')
        && s != "."
        && s != ".."
}
fn identity(i: &Identity) -> bool {
    i.pid > 0 && i.group > 0 && valid_id(&i.start)
}
impl Record {
    fn validate(&self, now_s: i64) -> Result<(), Refusal> {
        if self.schema_version != 1 || !valid_id(&self.session_id) || !valid_id(&self.run_id) {
            return Err(Refusal::Invalid);
        }
        if self.updated_at < 0 || now_s < self.updated_at {
            return Err(Refusal::Clock);
        }
        if let State::Busy(i) = &self.identity {
            if !identity(i) {
                return Err(Refusal::Invalid);
            }
        }
        if let State::Busy(t) = &self.pending {
            if !valid_id(&t.id) || matches!(&t.successor, Successor::Id(s) if !valid_id(s)) {
                return Err(Refusal::Invalid);
            }
        }
        let leases = [&self.pause, &self.finalizer].into_iter().filter_map(|s| {
            if let State::Busy(l) = s {
                Some(l)
            } else {
                None
            }
        });
        let runners = if let State::Busy(r) = &self.runners {
            r.as_slice()
        } else {
            &[]
        };
        if leases
            .chain(runners)
            .any(|l| !valid_id(&l.id) || !identity(&l.owner) || l.expires_at < 0)
        {
            return Err(Refusal::Invalid);
        }
        if self.identity == State::Free && !self.clear() {
            return Err(Refusal::Unresolved);
        }
        Ok(())
    }
    fn clear(&self) -> bool {
        self.pending == State::Free
            && self.pause == State::Free
            && self.finalizer == State::Free
            && self.runners == State::Free
    }
}
/// Pure comparison only: callers must later persist with durable CAS.
#[derive(Clone, Debug)]
pub enum Transition {
    /// Acquire a free, clear record.
    Acquire(Identity),
    /// Confirm a launched transaction; retain successor.
    Complete(String),
    /// Launch the matching prepared transaction.
    Launch(String, String),
    /// Release an occupied, clear record.
    Release,
}
pub fn advance(
    record: &Record,
    expected_epoch: u64,
    expected_identity: &State<Identity>,
    transition: Transition,
    now_s: i64,
) -> Result<Record, Refusal> {
    record.validate(now_s)?;
    if record.epoch != expected_epoch || &record.identity != expected_identity {
        return Err(Refusal::Invalid);
    }
    let mut next = record.clone();
    match transition {
        Transition::Acquire(i) if record.identity == State::Free && record.clear() => {
            next.identity = State::Busy(i);
        }
        Transition::Release if matches!(record.identity, State::Busy(_)) && record.clear() => {
            next.identity = State::Free;
        }
        Transition::Complete(id)
            if matches!(&record.pending, State::Busy(t) if t.id == id && t.phase == Phase::Launched && matches!(t.successor, Successor::Id(_)))
                && matches!(record.identity, State::Busy(_)) =>
        {
            if let State::Busy(t) = &mut next.pending {
                t.phase = Phase::Confirmed;
            }
        }
        Transition::Launch(id, successor)
            if matches!(&record.pending, State::Busy(t) if t.id == id && t.phase == Phase::Prepared && t.successor == Successor::Unknown)
                && matches!(record.identity, State::Busy(_)) =>
        {
            if let State::Busy(t) = &mut next.pending {
                t.phase = Phase::Launched;
                t.successor = Successor::Id(successor);
            }
        }
        _ => return Err(Refusal::Unresolved),
    }
    next.epoch = record.epoch.checked_add(1).ok_or(Refusal::Invalid)?;
    next.updated_at = now_s;
    next.validate(now_s)?;
    Ok(next)
}
/// Explicit bootstrap only; the caller must prove the slot has never existed.
pub fn bootstrap(session: &str, run: &str, now_s: i64) -> Result<Record, Refusal> {
    let r = Record {
        schema_version: 1,
        session_id: session.into(),
        run_id: run.into(),
        epoch: 0,
        identity: State::Free,
        updated_at: now_s,
        pending: State::Free,
        pause: State::Free,
        finalizer: State::Free,
        runners: State::Free,
    };
    r.validate(now_s)?;
    Ok(r)
}
/// Lexical confinement; root and each existing component must also be symlink-free.
pub fn slot_path(root: &Path, slot: &Path) -> Result<PathBuf, Refusal> {
    if slot.as_os_str().is_empty()
        || slot
            .components()
            .any(|c| !matches!(c, Component::Normal(_)))
    {
        return Err(Refusal::Invalid);
    }
    let mut p = root.to_path_buf();
    if std::fs::symlink_metadata(&p).is_ok_and(|m| m.file_type().is_symlink()) {
        return Err(Refusal::Invalid);
    }
    for c in slot.components() {
        p.push(c);
        if std::fs::symlink_metadata(&p).is_ok_and(|m| m.file_type().is_symlink()) {
            return Err(Refusal::Invalid);
        }
    }
    Ok(p)
}
/// Inject I/O results for deterministic unreadable/lost-record handling. Never bootstraps.
pub fn decode(session: &str, run: &str, bytes: io::Result<Vec<u8>>, now_s: i64) -> Read {
    decode_inner(session, run, bytes, now_s, false)
}
/// Explicitly validate v0; unknown lifecycle fields remain non-authorizing.
pub fn migrate_v0(
    session: &str,
    run: &str,
    bytes: io::Result<Vec<u8>>,
    now_s: i64,
) -> Result<Record, Refusal> {
    match decode_inner(session, run, bytes, now_s, true) {
        Read::Known(r) => Ok(r),
        Read::Unknown { .. } => Err(Refusal::Invalid),
    }
}
fn decode_inner(
    session: &str,
    run: &str,
    bytes: io::Result<Vec<u8>>,
    now_s: i64,
    migration: bool,
) -> Read {
    let result = (|| -> Result<Record, String> {
        let bytes = bytes.map_err(|e| format!("io:{:?}", e.kind()))?;
        let v: serde_json::Value = serde_json::from_slice(&bytes).map_err(|_| "malformed")?;
        let r = match v["schema_version"].as_u64() {
            Some(1) if !migration => {
                serde_json::from_slice::<Record>(&bytes).map_err(|_| "schema")?
            }
            Some(0) if !migration => return Err("migration required".into()),
            Some(0) => {
                #[derive(Deserialize)]
                #[serde(deny_unknown_fields)]
                struct V0 {
                    schema_version: u32,
                    session_id: String,
                    run_id: String,
                    epoch: u64,
                    identity: Identity,
                }
                let old: V0 = serde_json::from_slice(&bytes).map_err(|_| "migration")?;
                if old.schema_version != 0 {
                    return Err("migration".into());
                }
                Record {
                    schema_version: 1,
                    session_id: old.session_id,
                    run_id: old.run_id,
                    epoch: old.epoch,
                    identity: State::Busy(old.identity),
                    updated_at: now_s,
                    pending: State::Unknown,
                    pause: State::Unknown,
                    finalizer: State::Unknown,
                    runners: State::Unknown,
                }
            }
            _ => return Err("unsupported schema".into()),
        };
        if r.session_id != session || r.run_id != run {
            return Err("slot mismatch".into());
        }
        r.validate(now_s).map_err(|e| format!("{e:?}"))?;
        Ok(r)
    })();
    match result {
        Ok(r) => Read::Known(r),
        Err(reason) => Read::Unknown {
            session_id: session.into(),
            reason,
            alarm: true,
        },
    }
}
#[cfg(unix)]
fn confined_read(root: &Path, slot: &Path) -> io::Result<Vec<u8>> {
    use std::os::unix::fs::OpenOptionsExt;
    if slot.as_os_str().is_empty()
        || slot
            .components()
            .any(|c| !matches!(c, Component::Normal(_)))
    {
        return Err(io::ErrorKind::InvalidInput.into());
    }
    let root = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_DIRECTORY)
        .open(root)?;
    confined_read_at(root, slot)
}
#[cfg(unix)]
fn confined_read_at(mut dir: std::fs::File, slot: &Path) -> io::Result<Vec<u8>> {
    use std::{
        ffi::CString,
        io::Read,
        os::{
            fd::{AsRawFd, FromRawFd},
            unix::ffi::OsStrExt,
        },
    };
    let mut parts = slot.components().peekable();
    while let Some(part) = parts.next() {
        let name = CString::new(part.as_os_str().as_bytes())?;
        let flags = libc::O_RDONLY
            | libc::O_CLOEXEC
            | libc::O_NOFOLLOW
            | if parts.peek().is_some() {
                libc::O_DIRECTORY
            } else {
                0
            };
        // SAFETY: terminated name and live directory; successful descriptor is owned once.
        let fd = unsafe { libc::openat(dir.as_raw_fd(), name.as_ptr(), flags) };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        dir = unsafe { std::fs::File::from_raw_fd(fd) };
    }
    let mut bytes = Vec::new();
    dir.read_to_end(&mut bytes)?;
    Ok(bytes)
}
#[cfg(not(unix))]
fn confined_read(_: &Path, _: &Path) -> io::Result<Vec<u8>> {
    Err(io::ErrorKind::Unsupported.into())
}
pub fn read(root: &Path, slot: &Path, session: &str, run: &str, now_s: i64) -> Read {
    let bytes = confined_read(root, slot);
    decode(session, run, bytes, now_s)
}

#[rustfmt::skip]
#[cfg(test)]
mod tests {
    use super::*;
    fn advance(r: &Record, e: u64, i: &State<Identity>, t: Transition) -> Result<Record, Refusal> { super::advance(r, e, i, t, r.updated_at) }
    fn owner() -> Identity { Identity { pid: 7, start: "birth-a".into(), group: 7, } }
    fn encoded(r: &Record) -> io::Result<Vec<u8>> { Ok(serde_json::to_vec(r).unwrap()) }
    fn unknown(r: Read, reason: &str) { assert!(
            matches!(r, Read::Unknown { session_id, reason: why, alarm: true }
            if session_id == "session-a" && why.contains(reason)) ); }
    #[test]
    fn reconciliation_owner_is_documented() { assert!(include_str!("fleet_owner_lease.rs").lines().take(12).any(|l| l.contains("Part 2 owns explicit expected-epoch reconciliation"))); }
    #[test]
    fn phase_evidence_and_clock() {
        let mut r = bootstrap("session-a", "run-a", 10).unwrap(); r.identity = State::Busy(owner());
        r.pending = State::Busy(Transaction { id: "tx-a".into(), phase: Phase::Prepared, successor: Successor::Unknown });
        assert!(advance(&r, 0, &r.identity, Transition::Complete("tx-a".into())).is_err());
        let launched = super::advance(&r, 0, &r.identity, Transition::Launch("tx-a".into(), "next-a".into()), 11).unwrap();
        assert_eq!(launched.updated_at, 11);
        let done = advance(&launched, 1, &r.identity, Transition::Complete("tx-a".into())).unwrap();
        assert!(matches!(done.pending, State::Busy(Transaction { phase: Phase::Confirmed, successor: Successor::Id(ref id), .. }) if id == "next-a"));
        assert!(advance(&done, 2, &r.identity, Transition::Complete("tx-a".into())).is_err());
    }
    #[cfg(unix)]
    #[test]
    fn held_root_survives_symlink_swap() {
        let d = tempfile::tempdir().unwrap(); let root = d.path().join("dir"); let moved = d.path().join("moved");
        std::fs::create_dir(&root).unwrap(); std::fs::write(root.join("real.json"), b"held").unwrap();
        let held = std::fs::File::open(&root).unwrap(); std::fs::rename(&root, &moved).unwrap();
        std::fs::write(d.path().join("real.json"), b"escaped").unwrap(); std::os::unix::fs::symlink(d.path(), &root).unwrap();
        assert_eq!(confined_read_at(held, Path::new("real.json")).unwrap(), b"held");
        assert!(confined_read(&root, Path::new("real.json")).is_err());
    }
    #[test]
    fn advance_guards_and_exact_epoch() {
        let r = bootstrap("session-a", "run-a", 10).unwrap();
        assert!(advance(&r, 1, &State::Free, Transition::Acquire(owner())).is_err());
        let busy = advance(&r, 0, &State::Free, Transition::Acquire(owner())).unwrap();
        assert_eq!(busy.epoch, 1);
        assert!(advance(&busy, 0, &State::Free, Transition::Acquire(owner())).is_err());
        let mut reused = owner(); reused.start = "birth-b".into();
        assert!(advance(&busy, 1, &State::Busy(reused), Transition::Release).is_err());
        assert_eq!(advance(&busy, 1, &busy.identity, Transition::Release).unwrap().epoch, 2);
        let mut pending = busy.clone(); pending.pending = State::Busy(Transaction {
            id: "tx-a".into(), phase: Phase::Prepared, successor: Successor::Unknown });
        assert!(advance(&pending, 1, &pending.identity, Transition::Complete("tx-b".into())).is_err());
        assert!(advance(&pending, 1, &pending.identity, Transition::Complete("tx-a".into())).is_err());
        for now in [19, 20, 21] { for field in 0..4 {
            let mut blocked = busy.clone(); let l = Lease { id: "lease-a".into(), owner: owner(), expires_at: 20 };
            match field { 0 => blocked.pending = pending.pending.clone(), 1 => blocked.finalizer = State::Busy(l),
                2 => blocked.pause = State::Busy(l), _ => blocked.runners = State::Busy(vec![l]) }
            assert_eq!(decode("session-a", "run-a", encoded(&blocked), now), Read::Known(blocked.clone()));
            assert!(advance(&blocked, 1, &blocked.identity, Transition::Release).is_err());
        } }
    }
    #[test]
    fn json_paths_and_explicit_states() { let mut r = bootstrap("session-a", "run-a", 10).unwrap();
        assert_eq!(r.identity, State::Free); r.identity = State::Busy(owner());
        r.pending = State::Busy(Transaction { id: "tx-a".into(), phase: Phase::Launched,
            successor: Successor::Id("next-a".into()), }); let l = Lease { id: "lease-a".into(),
            owner: owner(), expires_at: 20, }; r.pause = State::Busy(l.clone());
        r.finalizer = State::Busy(l.clone()); r.runners = State::Busy(vec![l]); assert_eq!(
            decode("session-a", "run-a", encoded(&r), 10), Read::Known(r.clone()) );
        r.pending = State::Unknown; assert_eq!( decode("session-a", "run-a", encoded(&r), 10),
            Read::Known(r.clone()) ); let d = tempfile::tempdir().unwrap();
        let slot = Path::new("slot.json");
        assert_eq!(slot_path(d.path(), slot).unwrap(), d.path().join(slot));
        std::fs::write(d.path().join(slot), encoded(&r).unwrap()).unwrap(); assert_eq!(
            read(d.path(), slot, "session-a", "run-a", 10), Read::Known(r) );
        for bad in ["", ".", "..", "../escape", "/absolute"] {
            assert!(slot_path(d.path(), Path::new(bad)).is_err()); }
        for bad in ["", ".", "..", "bad/id", "bad\\id", "bad\n"] {
            assert!(bootstrap(bad, "run-a", 10).is_err());
            assert!(bootstrap("session-a", bad, 10).is_err()); } }
    #[test]
    fn loss_conflict_and_corruption_are_session_scoped() { let d = tempfile::tempdir().unwrap();
        let r = bootstrap("session-b", "run-b", 10).unwrap();
        std::fs::write(d.path().join("other"), encoded(&r).unwrap()).unwrap(); unknown(
            read(d.path(), Path::new("lost"), "session-a", "run-a", 10), "NotFound", ); unknown( decode(
                "session-a", "run-a", Err(io::ErrorKind::PermissionDenied.into()), 10, ),
            "PermissionDenied", ); unknown( read(d.path(), Path::new("."), "session-a", "run-a", 10),
            "InvalidInput", ); unknown( decode("session-a", "run-a", Ok(b"broken".to_vec()), 10),
            "malformed", ); let mut conflict = bootstrap("session-a", "run-a", 10).unwrap();
        conflict.pending = State::Busy(Transaction { id: "tx".into(), phase: Phase::Prepared,
            successor: Successor::Unknown, }); unknown(
            decode("session-a", "run-a", encoded(&conflict), 10), "Unresolved", );
        let mut v = serde_json::to_value(&r).unwrap(); v.as_object_mut().unwrap().remove("pause");
        unknown( decode( "session-a", "run-a", Ok(serde_json::to_vec(&v).unwrap()), 10, ), "schema", );
        assert_eq!( read(d.path(), Path::new("other"), "session-b", "run-b", 10), Read::Known(r) ); }
    #[test]
    fn identity_reading_never_infers_release() {
        let mut r = bootstrap("session-a", "run-a", 10).unwrap(); r.identity = State::Busy(owner());
        let mut reused = owner(); reused.start = "birth-b".into();
        assert_ne!(r.identity, State::Busy(reused)); assert_eq!(
            decode("session-a", "run-a", encoded(&r), 30), Read::Known(r.clone()) );
        unknown(decode("session-a", "run-a", encoded(&r), 9), "Clock"); r.identity = State::Unknown;
        assert_eq!( decode("session-a", "run-a", encoded(&r), 30), Read::Known(r.clone()) );
        r.identity = State::Busy(Identity { start: String::new(), ..owner() });
        unknown(decode("session-a", "run-a", encoded(&r), 30), "Invalid"); }
    #[test]
    fn migration_is_explicit_and_non_authorizing() {
        let mut v = serde_json::json!({"schema_version":0,"session_id":"session-a",
            "run_id":"run-a","epoch":42,"identity":owner()}); unknown(decode("session-a", "run-a", Ok(serde_json::to_vec(&v).unwrap()), 10), "migration required");
        let r = migrate_v0( "session-a",
            "run-a", Ok(serde_json::to_vec(&v).unwrap()), 10, ).unwrap();
        assert!(advance(&r, 42, &r.identity, Transition::Acquire(owner())).is_err());
        assert!(advance(&r, 42, &r.identity, Transition::Release).is_err());
        assert_eq!((r.epoch, r.identity.clone()), (42, State::Busy(owner()))); assert_eq!(
            (r.pending, r.pause, r.finalizer, r.runners), ( State::Unknown, State::Unknown,
                State::Unknown, State::Unknown ) ); v["identity"]["start"] = "".into(); assert!(migrate_v0("session-a", "run-a", Ok(serde_json::to_vec(&v).unwrap()), 10).is_err());
        v["schema_version"] = 2.into(); unknown( decode( "session-a", "run-a",
                Ok(serde_json::to_vec(&v).unwrap()), 10, ), "unsupported", ); } }
