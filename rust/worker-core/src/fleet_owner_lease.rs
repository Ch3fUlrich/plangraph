//! Unwired store contract: no writes, launches, kills, or lifecycle callers.
//! Later readers consume this record under their own keys; none is in this slice.
//! JSON uses schema_version=1 and externally tagged Unknown/Free/Busy states;
//! every lifecycle field is required. Identity includes pid, start and group.
//! v0 decoding preserves identity/epoch but requires explicit reconciliation.
//! Python consumers read the JSON.
//! Paths are caller-supplied relative slots, never derived from opaque IDs.
//! Part 2 owns comparison and reconciliation; part 3 owns filesystem CAS.
//! Known is validated data, not ownership permission; Unknown never authorizes.
//! Neither expiry nor process death frees a slot.
use serde::{Deserialize, Serialize};
use std::{
    io,
    path::{Component, Path, PathBuf},
};

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum State<T> {
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
    Id(String),
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Phase {
    Prepared,
    Launched,
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
    pub reconciliation_required: bool,
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
    /// Expiration is evidence for later reconciliation, never permission to own.
    pub fn expired_leases(&self, now_s: i64) -> Vec<&Lease> {
        let runners = match &self.runners {
            State::Busy(r) => r.as_slice(),
            _ => &[],
        };
        [&self.pause, &self.finalizer]
            .into_iter()
            .filter_map(|s| match s {
                State::Busy(l) => Some(l),
                _ => None,
            })
            .chain(runners)
            .filter(|l| now_s >= l.expires_at)
            .collect()
    }
    pub(crate) fn validate(&self, now_s: i64) -> Result<(), Refusal> {
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
    pub(crate) fn clear(&self) -> bool {
        self.pending == State::Free
            && self.pause == State::Free
            && self.finalizer == State::Free
            && self.runners == State::Free
    }
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
        reconciliation_required: false,
        pending: State::Free,
        pause: State::Free,
        finalizer: State::Free,
        runners: State::Free,
    };
    r.validate(now_s)?;
    Ok(r)
}
fn lexical_slot(slot: &Path) -> Result<(), Refusal> {
    if slot.as_os_str().is_empty()
        || slot
            .components()
            .any(|c| !matches!(c, Component::Normal(_)))
        || slot.as_os_str().as_encoded_bytes().contains(&0)
    {
        return Err(Refusal::Invalid);
    }
    Ok(())
}
/// Lexical confinement only; does not inspect the filesystem.
pub fn slot_path(root: &Path, slot: &Path) -> Result<PathBuf, Refusal> {
    lexical_slot(slot)?;
    Ok(root.join(slot))
}
/// Inject I/O results for deterministic unreadable/lost-record handling. Never bootstraps.
pub fn decode(session: &str, run: &str, bytes: io::Result<Vec<u8>>, now_s: i64) -> Read {
    decode_inner(session, run, bytes, now_s, false)
}
/// Explicitly validate v0; unknown lifecycle fields remain non-authorizing.
pub fn migrate_v0(session: &str, run: &str, bytes: io::Result<Vec<u8>>, now_s: i64) -> Read {
    decode_inner(session, run, bytes, now_s, true)
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
                    reconciliation_required: true,
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
    lexical_slot(slot).map_err(|_| io::Error::from(io::ErrorKind::InvalidInput))?;
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
        let name = CString::new(part.as_os_str().as_bytes())
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidInput, e))?;
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
#[cfg(windows)]
fn confined_read(root: &Path, slot: &Path) -> io::Result<Vec<u8>> {
    use std::{fs::OpenOptions, io::Read, os::windows::fs::OpenOptionsExt};
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_READ,
    };
    lexical_slot(slot).map_err(|_| io::Error::from(io::ErrorKind::InvalidInput))?;
    // Keep every ancestor open without delete/write sharing: no component can
    // be renamed or changed into a reparse point while resolving its children.
    let mut held = Vec::new();
    let mut path = PathBuf::new();
    let full = root.join(slot);
    let mut parts = full.components().peekable();
    while let Some(part) = parts.next() {
        path.push(part.as_os_str());
        if matches!(part, Component::Prefix(_) | Component::RootDir) {
            continue;
        }
        let mut file = OpenOptions::new()
            .read(true)
            .share_mode(FILE_SHARE_READ)
            .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
            .open(&path)?;
        use std::os::windows::fs::MetadataExt;
        let meta = file.metadata()?;
        if meta.file_attributes() & 0x400 != 0 {
            return Err(io::ErrorKind::PermissionDenied.into());
        }
        if parts.peek().is_none() {
            let mut bytes = Vec::new();
            file.read_to_end(&mut bytes)?;
            return Ok(bytes);
        }
        if !meta.is_dir() {
            return Err(io::ErrorKind::NotADirectory.into());
        }
        held.push(file);
    }
    Err(io::ErrorKind::InvalidInput.into())
}
#[cfg(not(any(unix, windows)))]
fn confined_read(_: &Path, _: &Path) -> io::Result<Vec<u8>> {
    Err(io::ErrorKind::Unsupported.into())
}
pub fn read(root: &Path, slot: &Path, session: &str, run: &str, now_s: i64) -> Read {
    let bytes = confined_read(root, slot);
    decode(session, run, bytes, now_s)
}

#[cfg(test)]
#[path = "fleet_owner_lease_tests.rs"]
mod tests;
