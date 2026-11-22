use super::{
    paths::{basename, io_text, resolve, var, Paths},
    policy::{attended_window, Admission},
    Caps, Request,
};
use serde::Serialize;
use serde_json::{json, Value};
use std::{
    ffi::CString,
    fs::{self, DirBuilder, File, OpenOptions},
    io::{self, Read, Write},
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::{
            ffi::OsStrExt,
            fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt},
        },
    },
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

pub(super) fn epoch_millis() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
}

fn timestamp() -> u64 {
    (epoch_millis() / 1000) as u64
}

pub(super) fn next_seq(state: &Path) -> io::Result<u64> {
    fs::create_dir_all(state)?;
    let mut file = OpenOptions::new()
        .read(true)
        .append(true)
        .create(true)
        .open(state.join("session-seq"))?;
    flock(&file, libc::LOCK_EX)?;
    let mut old = String::new();
    file.read_to_string(&mut old)?;
    let n = old
        .trim()
        .parse::<u64>()
        .unwrap_or(0)
        .checked_add(1)
        .ok_or_else(|| io::Error::other("session counter overflow"))?;
    file.set_len(0)?;
    write!(file, "{n}")?;
    Ok(n)
}

pub(super) fn role_lock(state: &Path, role: &str) -> Result<File, String> {
    let dir = state.join("role-locks");
    let path = dir.join(format!("{role}.lock"));
    fs::create_dir_all(&dir)
        .map_err(|e| format!("role lock unavailable: {}", io_text(&e, &dir)))?;
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(&path)
        .map_err(|e| format!("role lock unavailable: {}", io_text(&e, &path)))?;
    flock(&file, libc::LOCK_EX | libc::LOCK_NB).map_err(|e| {
        if e.kind() == io::ErrorKind::WouldBlock {
            "another launcher holds the role lock".into()
        } else {
            format!("role lock unavailable: {}", io_text(&e, &path))
        }
    })?;
    Ok(file)
}

fn flock(file: &File, operation: libc::c_int) -> io::Result<()> {
    loop {
        // SAFETY: the descriptor remains open throughout the call.
        if unsafe { libc::flock(file.as_raw_fd(), operation) } == 0 {
            return Ok(());
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(error);
        }
    }
}

pub(super) fn record_outcome(admission: &Admission, outcome: &str, name: Option<&str>) -> bool {
    if ledger(admission, false, outcome, name).is_err() {
        eprintln!("fleet-launch: admission ledger unavailable");
        return false;
    }
    true
}

pub(super) fn ledger(
    admission: &Admission,
    fallback: bool,
    outcome: &str,
    name: Option<&str>,
) -> io::Result<()> {
    let path = if fallback {
        admission.state.join("fleet-launch-fallback.jsonl")
    } else {
        admission.ledger.clone()
    };
    let row = json!({
        "ts": timestamp(), "result": outcome, "enforce": admission.enforce,
        "missing": admission.missing, "policy_error": admission.error,
        "cap": admission.caps.caps, "cap_source": admission.caps.source, "cap_error": admission.caps.error,
        "fields": admission.fields, "session_name": name
    });
    append(&admission.state, &path, &row)
}

fn append(state: &Path, path: &Path, row: &Value) -> io::Result<()> {
    let relative = path.strip_prefix(state).map_err(io::Error::other)?;
    let mut parts = relative.components();
    let leaf = parts
        .next_back()
        .ok_or_else(|| io::Error::other("missing ledger name"))?;
    DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(state)?;
    let mut parent = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECTORY)
        .open(state)?;
    for part in parts {
        let name = c_filename(part.as_os_str().as_bytes())?;
        // SAFETY: parent refers to an open directory and name is NUL terminated.
        if unsafe { libc::mkdirat(parent.as_raw_fd(), name.as_ptr(), 0o700) } != 0 {
            let error = io::Error::last_os_error();
            if error.kind() != io::ErrorKind::AlreadyExists {
                return Err(error);
            }
        }
        parent = open_at(&parent, &name, libc::O_RDONLY | libc::O_DIRECTORY, 0)?;
    }
    let name = c_filename(leaf.as_os_str().as_bytes())?;
    let mut file = open_at(
        &parent,
        &name,
        libc::O_WRONLY | libc::O_CREAT | libc::O_APPEND | libc::O_NONBLOCK,
        0o600,
    )?;
    parent.sync_all()?;
    if !file.metadata()?.is_file() {
        return Err(io::Error::other("ledger is not a regular file"));
    }
    flock(&file, libc::LOCK_EX)?;
    file.set_permissions(fs::Permissions::from_mode(0o600))?;
    writeln!(file, "{row}")?;
    file.sync_all()
}

fn c_filename(bytes: &[u8]) -> io::Result<CString> {
    CString::new(bytes).map_err(io::Error::other)
}

fn open_at(
    parent: &File,
    name: &CString,
    flags: libc::c_int,
    mode: libc::mode_t,
) -> io::Result<File> {
    // SAFETY: parent is open and name is NUL terminated. openat returns an owned descriptor.
    let fd = unsafe {
        libc::openat(
            parent.as_raw_fd(),
            name.as_ptr(),
            flags | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            mode,
        )
    };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: no other Rust object owns the successful openat result.
    Ok(unsafe { File::from_raw_fd(fd) })
}

pub(super) fn atomic_json<T: Serialize>(path: &Path, value: &T, prefix: &str) -> io::Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| io::Error::other("record directory missing"))?;
    fs::create_dir_all(parent)?;
    let mut file = tempfile::Builder::new()
        .prefix(prefix)
        .tempfile_in(parent)?;
    let fmt = serde_json::ser::PrettyFormatter::with_indent(b" ");
    value.serialize(&mut serde_json::Serializer::with_formatter(&mut file, fmt))?;
    file.persist(path).map_err(|e| e.error)?;
    Ok(())
}

pub(super) fn record_path(request: &Request) -> PathBuf {
    if request.args.respawn && var("FLEET_DEFER_RECORD").as_deref() == Some("1") {
        return request.spec_path.with_extension("pending.json");
    }
    request.spec_path.clone()
}

pub(super) fn register_child(state: &Path, name: &str, spec: &Value) {
    let Some(parent) = spec["parent"].as_str().filter(|s| !s.is_empty()) else {
        return;
    };
    let parent_role = super::role_of(parent);
    if !basename(parent_role) || !basename(name) {
        return;
    }
    let dir = state.join("launch/children");
    let path = dir.join(format!("{parent_role}.txt"));
    let result = fs::create_dir_all(&dir)
        .and_then(|()| {
            OpenOptions::new()
                .append(true)
                .create(true)
                .mode(0o600)
                .open(&path)
        })
        .and_then(|mut f| f.write_all(format!("{name}\n").as_bytes()));
    if let Err(error) = result {
        eprintln!(
            "fleet-launch: warning: {name} not added to {parent} children: {}",
            io_text(&error, &path)
        );
    }
}

pub(super) fn publish(
    request: &Request,
    paths: &Paths,
    admission: &Admission,
    mcp: &Path,
    name: &str,
    seq: u64,
    sid: Option<&str>,
) -> io::Result<Value> {
    let args = &request.args;
    let mut spec = request.spec.clone();
    if spec.as_object().unwrap().is_empty() {
        spec = json!({
            "cwd": args.cwd, "mcp": mcp, "prompt_file": resolve(Path::new(&args.prompt)), "children": [],
        });
    }
    for key in super::arguments::METADATA {
        spec[key] = admission.fields[key].clone();
    }
    if let Some(parent) = args.option("parent") {
        let parent = super::role_of(parent);
        spec["parent"] = if parent.is_empty() {
            Value::Null
        } else {
            json!(parent)
        };
    } else if spec.get("parent").is_none() {
        spec["parent"] = Value::Null;
    }
    if args.oom {
        spec["oom_protect"] = json!(true);
    }
    if let Some(guard) = request
        .override_cap
        .as_ref()
        .filter(|cap| Caps::from_value(cap).is_some())
    {
        spec["ctx_guard"] = guard.clone();
        spec["ctx_guard_source"] = json!("operator");
    } else {
        let object = spec.as_object_mut().unwrap();
        object.remove("ctx_guard");
        object.remove("ctx_guard_source");
    }
    spec["launched_caps"] = json!(admission.caps.caps);
    spec["attended_window_s"] = json!(admission.caps.window);
    spec["model"] = json!(args.model);
    spec["effort"] = json!(args.effort);
    spec["ts"] = json!(timestamp());
    spec["name"] = json!(args.role);
    spec["session_name"] = json!(name);
    spec["seq"] = json!(seq);
    spec["session_id"] = json!(sid);
    spec["job_id"] = json!(sid.map(|id| &id[..8]));
    let path = record_path(request);
    atomic_json(&path, &spec, ".launch-")?;
    atomic_json(
        &paths
            .state
            .join("launch/sessions")
            .join(format!("{name}.json")),
        &spec,
        ".launch-",
    )?;
    if path == request.spec_path {
        register_child(&paths.state, name, &spec);
    }
    Ok(spec)
}

pub(super) fn setup(
    paths: &Paths,
    spec: &Value,
    cap: Caps,
    sid: &str,
    name: &str,
) -> io::Result<()> {
    if spec["oom_protect"].as_bool() == Some(true) {
        let directory = var("OOM_STATE_DIR").map(PathBuf::from).unwrap_or_else(|| {
            var("XDG_STATE_HOME")
                .map(PathBuf::from)
                .unwrap_or_else(|| paths.home.join(".local/state"))
                .join("routing")
        });
        fs::create_dir_all(&directory)?;
        let file = directory.join("oom-protect-sessions");
        let have = match fs::read_to_string(&file) {
            Ok(text) => text,
            Err(error) if error.kind() == io::ErrorKind::NotFound => String::new(),
            Err(error) => return Err(error),
        };
        if !have
            .lines()
            .any(|line| line.split_whitespace().next() == Some(sid))
        {
            writeln!(
                OpenOptions::new().append(true).create(true).open(file)?,
                "{sid} {name}"
            )?;
        }
    }
    let dir = var("CTX_GUARD_SESSION_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            paths
                .home
                .join(".local/state/claude-hooks/ctx-guard-session")
        });
    fs::create_dir_all(&dir)?;
    let temp = dir.join(format!("{sid}.json.tmp"));
    fs::write(
        &temp,
        json!({"handoff": cap.handoff, "hard": cap.hard,
        "attended_window_s": attended_window(spec.get("attended_window_s"))})
        .to_string(),
    )?;
    fs::rename(temp, dir.join(format!("{sid}.json")))
}
