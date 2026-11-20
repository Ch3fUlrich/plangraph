//! Keeper-local Claude Code transcript witness. Paths and diagnostics never leave the helper.
use serde_json::{json, Value};
use std::{
    collections::HashSet,
    io::Read,
    path::{Component, Path},
};

const MAX: u64 = 16 * 1024 * 1024;

// Walk with openat/O_NOFOLLOW: neither an intermediate nor a final symlink may
// redirect the read. Files must belong to the keeper and be non-writable by peers.
#[cfg(unix)]
fn read(path: &Path, missing: &'static str) -> Result<String, &'static str> {
    use std::{
        ffi::CString,
        fs::File,
        os::{
            fd::{AsRawFd, FromRawFd},
            unix::fs::MetadataExt,
        },
    };
    if !path.is_absolute() {
        return Err("claude_seat_path");
    }
    let mut parent = File::open("/").map_err(|_| "claude_seat_path")?;
    let parts: Vec<_> = path
        .components()
        .filter(|c| *c != Component::RootDir)
        .collect();
    for (i, part) in parts.iter().enumerate() {
        let Component::Normal(name) = part else {
            return Err("claude_seat_path");
        };
        use std::os::unix::ffi::OsStrExt;
        let name = CString::new(name.as_bytes()).map_err(|_| "claude_seat_path")?;
        let last = i + 1 == parts.len();
        let flags = libc::O_RDONLY
            | libc::O_NOFOLLOW
            | libc::O_NONBLOCK
            | libc::O_CLOEXEC
            | if last { 0 } else { libc::O_DIRECTORY };
        // SAFETY: parent and name remain valid for this call; a successful fd is owned below.
        let fd = unsafe { libc::openat(parent.as_raw_fd(), name.as_ptr(), flags) };
        if fd < 0 {
            return Err(missing);
        }
        // SAFETY: openat returned a fresh, owned descriptor.
        parent = unsafe { File::from_raw_fd(fd) };
    }
    let info = parent.metadata().map_err(|_| missing)?;
    // SAFETY: geteuid has no preconditions.
    if !info.is_file() || info.uid() != unsafe { libc::geteuid() } || info.mode() & 0o022 != 0 {
        return Err("claude_seat_untrusted_file");
    }
    let mut text = String::new();
    parent
        .take(MAX + 1)
        .read_to_string(&mut text)
        .map_err(|_| missing)?;
    if text.len() as u64 > MAX {
        return Err("claude_seat_size");
    }
    Ok(text)
}
#[cfg(not(unix))]
fn read(_: &Path, _: &'static str) -> Result<String, &'static str> {
    Err("claude_seat_platform")
}

fn field<'a>(v: &'a Value, key: &str) -> Result<&'a str, &'static str> {
    v[key]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or("claude_seat_ref")
}
fn verify(r: &Value) -> Result<Value, &'static str> {
    let head = field(r, "head")?;
    if ![40, 64].contains(&head.len())
        || !head
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
    {
        return Err("claude_seat_head");
    }
    let writers = r["writer_families"]
        .as_array()
        .filter(|a| !a.is_empty())
        .ok_or("claude_seat_writer_unknown")?;
    for w in writers {
        match w.as_str() {
            Some("claude" | "anthropic") => return Err("claude_seat_writer_family"),
            Some(s) if !s.is_empty() => (),
            _ => return Err("claude_seat_writer_unknown"),
        }
    }
    let root = Path::new(field(r, "root")?);
    let path = Path::new(field(r, "transcript")?);
    let relative = path.strip_prefix(root).map_err(|_| "claude_seat_path")?;
    let parts: Vec<_> = relative.components().collect();
    if parts.len() != 4
        || parts.iter().any(|c| !matches!(c, Component::Normal(_)))
        || parts[2].as_os_str() != "subagents"
    {
        return Err("claude_seat_path");
    }
    let session = parts[1].as_os_str().to_str().ok_or("claude_seat_path")?;
    let agent = parts[3]
        .as_os_str()
        .to_str()
        .and_then(|s| s.strip_prefix("agent-"))
        .and_then(|s| s.strip_suffix(".jsonl"))
        .filter(|s| !s.is_empty())
        .ok_or("claude_seat_path")?;
    let transcript = read(path, "claude_seat_transcript_missing")?;
    let review_path = field(r, "review_file")?;
    let review = read(Path::new(review_path), "claude_seat_review_missing")?;
    let lines: Vec<_> = review.lines().filter(|l| !l.trim().is_empty()).collect();
    if lines.last() != Some(&"VERDICT: ACCEPT")
        || lines.iter().filter(|l| l.starts_with("VERDICT:")).count() != 1
    {
        return Err("claude_seat_verdict");
    }
    if lines.len() < 2
        || lines[lines.len() - 2] != format!("HEAD: {head}")
        || lines.iter().filter(|l| l.starts_with("HEAD:")).count() != 1
    {
        return Err("claude_seat_head");
    }
    let mut model = None;
    let mut writes = HashSet::new();
    let mut completed = false;
    for line in transcript.lines() {
        let e: Value = serde_json::from_str(line).map_err(|_| "claude_seat_transcript_invalid")?;
        if !matches!(e["type"].as_str(), Some("assistant" | "user")) {
            continue;
        }
        if e["sessionId"] != session || e["agentId"] != agent {
            return Err("claude_seat_session");
        }
        let assistant = e["type"] == "assistant";
        if assistant {
            let m = field(&e["message"], "model").map_err(|_| "claude_seat_model")?;
            if !m.strip_prefix("claude-sonnet-").is_some_and(|s| {
                s.starts_with(|c: char| c.is_ascii_digit())
                    && s.chars()
                        .all(|c| c.is_ascii_digit() || c == '-' || c == '.')
            }) {
                return Err("claude_seat_model");
            }
            model = Some(m.to_owned());
        }
        for block in e["message"]["content"].as_array().into_iter().flatten() {
            if assistant
                && block["type"] == "tool_use"
                && block["name"] == "Write"
                && block["input"]["file_path"] == review_path
                && block["input"]["content"] == review
            {
                writes.insert(field(block, "id")?.to_owned());
            }
            if !assistant
                && block["type"] == "tool_result"
                && block.get("is_error").is_none_or(|v| v == false)
                && block["tool_use_id"]
                    .as_str()
                    .is_some_and(|id| writes.contains(id))
            {
                completed = true;
            }
        }
    }
    if !completed {
        return Err("claude_seat_write_unwitnessed");
    }
    Ok(
        json!({"model":model.ok_or("claude_seat_model")?, "head":head,
        "verdict":"pass", "run_id":format!("claude-seat-{session}-{agent}")}),
    )
}
fn main() {
    let mut body = String::new();
    let result = std::io::stdin()
        .take(MAX + 1)
        .read_to_string(&mut body)
        .map_err(|_| "claude_seat_ref")
        .and_then(|_| {
            if body.len() as u64 > MAX {
                return Err("claude_seat_size");
            }
            let r = serde_json::from_str(&body).map_err(|_| "claude_seat_ref")?;
            verify(&r)
        });
    println!(
        "{}",
        match result {
            Ok(seat) => json!({"ok":true, "detail":"claude_seat_verified", "seat":seat}),
            Err(reason) => json!({"ok":false, "detail":reason}),
        }
    );
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use serde_json::json;
    use std::{fs, path::PathBuf};

    // The trust check rejects group/other-writable files; a 0002 umask must not matter.
    fn write_private(path: impl AsRef<std::path::Path>, data: impl AsRef<[u8]>) {
        use std::os::unix::fs::PermissionsExt;
        fs::write(path.as_ref(), data).unwrap();
        fs::set_permissions(path.as_ref(), fs::Permissions::from_mode(0o600)).unwrap();
    }

    struct Fixture {
        _dir: tempfile::TempDir,
        request: serde_json::Value,
        events: Vec<serde_json::Value>,
    }
    impl Fixture {
        fn new() -> Self {
            let dir = tempfile::tempdir().unwrap();
            let root = dir.path().join("projects");
            let transcript = root.join("project/session/subagents/agent-seat.jsonl");
            fs::create_dir_all(transcript.parent().unwrap()).unwrap();
            let review = dir.path().join("review.md");
            let head = "a".repeat(40);
            let text = format!("Review complete.\nHEAD: {head}\nVERDICT: ACCEPT\n");
            write_private(&review, &text);
            let events = vec![
                json!({"type":"assistant", "sessionId":"session", "agentId":"seat",
                    "message":{"model":"claude-sonnet-5-5", "content":[
                        {"type":"tool_use", "id":"write-1", "name":"Write",
                         "input":{"file_path":review, "content":text}}]}}),
                json!({"type":"user", "sessionId":"session", "agentId":"seat",
                    "message":{"content":[{"type":"tool_result", "tool_use_id":"write-1",
                        "content":"File created successfully."}]}}),
            ];
            let mut f = Self {
                _dir: dir,
                events,
                request: json!({
                "root":root, "transcript":transcript, "review_file":review,
                "head":head, "writer_families":["openai"]}),
            };
            f.save();
            f
        }
        fn path(&self, key: &str) -> PathBuf {
            PathBuf::from(self.request[key].as_str().unwrap())
        }
        fn save(&mut self) {
            write_private(
                self.path("transcript"),
                self.events
                    .iter()
                    .map(|v| format!("{v}\n"))
                    .collect::<String>(),
            );
        }
    }

    #[test]
    fn sonnet_transcript_writes_accept_for_head() {
        let f = Fixture::new();
        let seat = verify(&f.request).unwrap();
        assert_eq!(seat["model"], "claude-sonnet-5-5");
        assert_eq!(seat["head"], f.request["head"]);
    }

    #[test]
    fn successful_write_and_unchanged_file_are_required() {
        let mut f = Fixture::new();
        f.events[1]["message"]["content"][0]["is_error"] = json!(true);
        f.save();
        assert_eq!(
            verify(&f.request).unwrap_err(),
            "claude_seat_write_unwitnessed"
        );
        f.events.pop();
        f.save();
        assert_eq!(
            verify(&f.request).unwrap_err(),
            "claude_seat_write_unwitnessed"
        );
        let f = Fixture::new();
        let text = fs::read_to_string(f.path("review_file")).unwrap();
        fs::write(f.path("review_file"), format!("Changed.\n{text}")).unwrap();
        assert_eq!(
            verify(&f.request).unwrap_err(),
            "claude_seat_write_unwitnessed"
        );
    }

    #[cfg(unix)]
    #[test]
    fn symlinked_transcript_is_refused() {
        let f = Fixture::new();
        let transcript = f.path("transcript");
        let moved = transcript.with_extension("moved");
        fs::rename(&transcript, &moved).unwrap();
        std::os::unix::fs::symlink(&moved, &transcript).unwrap();
        assert_eq!(
            verify(&f.request).unwrap_err(),
            "claude_seat_transcript_missing"
        );
    }

    #[test]
    fn claude_seat_refusals_are_named() {
        for model in ["claude-haiku-4-5", "gpt-6", "claude-sonnet-fake"] {
            let mut f = Fixture::new();
            f.events[0]["message"]["model"] = json!(model);
            f.save();
            assert_eq!(verify(&f.request).unwrap_err(), "claude_seat_model");
        }
        let f = Fixture::new();
        fs::remove_file(f.path("transcript")).unwrap();
        assert_eq!(
            verify(&f.request).unwrap_err(),
            "claude_seat_transcript_missing"
        );
        let f = Fixture::new();
        fs::remove_file(f.path("review_file")).unwrap();
        assert_eq!(
            verify(&f.request).unwrap_err(),
            "claude_seat_review_missing"
        );
        let mut f = Fixture::new();
        f.events[0]["sessionId"] = json!("another-session");
        f.save();
        assert_eq!(verify(&f.request).unwrap_err(), "claude_seat_session");
        let mut f = Fixture::new();
        f.events[0]["message"]["content"][0]["input"]["file_path"] = json!("elsewhere.md");
        f.save();
        assert_eq!(
            verify(&f.request).unwrap_err(),
            "claude_seat_write_unwitnessed"
        );
        let mut f = Fixture::new();
        f.request["head"] = json!("b".repeat(40));
        assert_eq!(verify(&f.request).unwrap_err(), "claude_seat_head");
        let f = Fixture::new();
        fs::write(
            f.path("review_file"),
            format!("HEAD: {}\nVERDICT: REQUEST_CHANGES\n", "a".repeat(40)),
        )
        .unwrap();
        assert_eq!(verify(&f.request).unwrap_err(), "claude_seat_verdict");
        let mut f = Fixture::new();
        f.request["writer_families"] = json!(["anthropic"]);
        assert_eq!(verify(&f.request).unwrap_err(), "claude_seat_writer_family");
    }
}
