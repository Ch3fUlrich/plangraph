//! Match session transcript failures to individual entries in the lesson ledger.
use serde_json::Value;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs::{self, File};
use std::io::{self, BufRead, BufReader, Write};
use std::path::PathBuf;

pub fn run(input: impl BufRead, diagnostic: &mut impl Write) -> i32 {
    let payload: Value = match serde_json::from_reader(input) {
        Ok(payload) => payload,
        Err(error) => {
            let _ = writeln!(diagnostic, "lesson-stop: invalid hook input: {error}");
            return 1;
        }
    };
    if payload["hook_event_name"] != "Stop" {
        return 0;
    }
    match pending(&payload) {
        Ok(pending) if pending.is_empty() => 0,
        Ok(pending) => {
            for (id, reason) in pending {
                let _ = writeln!(diagnostic, "{id} {reason}");
            }
            let _ = writeln!(diagnostic, "lesson-stop: record each signal ID in the session ledger with an LE key or attached to <key>");
            2
        }
        Err(error) => {
            let _ = writeln!(
                diagnostic,
                "lesson-stop: cannot check the session ledger: {error}"
            );
            2
        }
    }
}

fn pending(payload: &Value) -> io::Result<BTreeMap<String, &'static str>> {
    let session = payload["session_id"]
        .as_str()
        .filter(|id| simple_id(id))
        .ok_or_else(|| io::Error::other("missing or invalid session_id"))?;
    let transcript = payload["transcript_path"]
        .as_str()
        .ok_or_else(|| io::Error::other("missing transcript_path"))?;
    let mut found = Failures::default();
    for (index, line) in BufReader::new(File::open(transcript)?).lines().enumerate() {
        let line = line?;
        if !line.trim().is_empty() {
            found.scan(&serde_json::from_str(&line)?, index + 1);
        }
    }
    if !found.signals.is_empty() {
        let dir = std::env::var_os("PG_LESSON_LEDGER_DIR")
            .map(PathBuf::from)
            .or_else(|| {
                std::env::var_os("XDG_STATE_HOME")
                    .map(|p| PathBuf::from(p).join("plangraph/lessons"))
            })
            .or_else(|| {
                std::env::var_os("HOME")
                    .map(|p| PathBuf::from(p).join(".local/state/plangraph/lessons"))
            })
            .ok_or_else(|| io::Error::other("set PG_LESSON_LEDGER_DIR"))?;
        let ledger = match fs::read_to_string(dir.join(format!("lesson-{session}.tsv"))) {
            Ok(ledger) => ledger,
            Err(error) if error.kind() == io::ErrorKind::NotFound => String::new(),
            Err(error) => return Err(error),
        };
        let resolved: HashSet<_> = ledger.lines().filter_map(ledger_entry).collect();
        found
            .signals
            .retain(|id, _| !resolved.contains(id.as_str()));
    }
    Ok(found.signals)
}

fn simple_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 128
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

fn keeper_key(key: &str) -> bool {
    let item = match key.split_once(':') {
        Some((project, item)) if simple_id(project) => item,
        Some(_) => return false,
        None => key,
    };
    simple_id(item)
        && item.bytes().any(|b| b.is_ascii_uppercase())
        && item.bytes().all(|b| !b.is_ascii_lowercase())
}

fn ledger_entry(line: &str) -> Option<&str> {
    let (id, resolution) = line.split_once('\t')?;
    let resolution = resolution.trim();
    let valid = if let Some(key) = resolution.strip_prefix("attached to ") {
        keeper_key(key)
    } else {
        keeper_key(resolution)
            && resolution
                .rsplit(':')
                .next()
                .is_some_and(|item| item.starts_with("LE-") && item.len() > 3)
    };
    valid.then_some(id)
}

#[derive(Default)]
struct Failures {
    tools: HashMap<String, String>,
    signals: BTreeMap<String, &'static str>,
}

impl Failures {
    fn scan(&mut self, row: &Value, line: usize) {
        if row["isSidechain"] == true {
            return;
        }
        match row["type"].as_str() {
            Some("assistant" | "user") => {
                let content = &row["message"]["content"];
                if let Some(blocks) = content.as_array() {
                    for (index, block) in blocks.iter().enumerate() {
                        match block["type"].as_str() {
                            Some("tool_use") if row["type"] == "assistant" => {
                                self.call(&block["id"], &block["name"])
                            }
                            Some("tool_result") if row["type"] == "user" => {
                                self.result(block, &block["tool_use_id"], &block["content"])
                            }
                            Some("text") if row["type"] == "user" => {
                                self.message(&block["text"], line, index + 1)
                            }
                            _ => {}
                        }
                    }
                } else if row["type"] == "user" {
                    self.message(content, line, 1);
                }
            }
            Some("response_item") => {
                let frame = &row["payload"];
                match frame["type"].as_str() {
                    Some("function_call" | "custom_tool_call") => {
                        self.call(&frame["call_id"], &frame["name"])
                    }
                    Some("function_call_output" | "custom_tool_call_output") => {
                        self.result(frame, &frame["call_id"], &frame["output"])
                    }
                    _ => {}
                }
            }
            Some("event_msg") if row["payload"]["type"] == "user_message" => {
                self.message(&row["payload"]["message"], line, 1);
            }
            Some("event_msg") if row["payload"]["type"] == "exec_command_end" => {
                let frame = &row["payload"];
                self.result(frame, &frame["call_id"], &frame["aggregated_output"]);
            }
            _ => {}
        }
    }

    fn call(&mut self, id: &Value, name: &Value) {
        if let (Some(id), Some(name)) = (id.as_str(), name.as_str()) {
            self.tools.insert(id.to_owned(), name.to_ascii_lowercase());
        }
    }

    fn result(&mut self, frame: &Value, id: &Value, output: &Value) {
        let Some(id) = id.as_str().filter(|id| simple_id(id)) else {
            return;
        };
        let keeper = self
            .tools
            .get(id)
            .is_some_and(|name| name.contains("keeper"));
        let reason = if keeper && (failed(frame) || has_text(output, keeper_refusal)) {
            Some("keeper refusal or timeout")
        } else if failed(frame) || has_text(output, nonzero_exit) {
            Some("tool failure")
        } else {
            None
        };
        if let Some(reason) = reason {
            self.signals.insert(format!("tool:{id}"), reason);
        }
    }

    fn message(&mut self, text: &Value, line: usize, block: usize) {
        let Some(text) = text.as_str().map(str::trim_start) else {
            return;
        };
        let reason = if let Some(message) = text.strip_prefix("<teammate-message ") {
            let Some((_, body)) = message.split_once('>') else {
                return;
            };
            match marker(body) {
                "BLOCKED" => "peer BLOCKED",
                "FAIL" => "peer FAIL",
                "CORRECTION" => "peer CORRECTION",
                _ => return,
            }
        } else if marker(text).eq_ignore_ascii_case("CORRECTION") {
            "operator correction"
        } else {
            return;
        };
        self.signals
            .insert(format!("message:{line}:{block}"), reason);
    }
}

fn marker(text: &str) -> &str {
    text.trim_start()
        .split(|c: char| c.is_whitespace() || c == ':')
        .next()
        .unwrap_or("")
}

fn failed(frame: &Value) -> bool {
    frame["is_error"] == true
        || frame["isError"] == true
        || frame["exit_code"].as_i64().is_some_and(|code| code != 0)
}

fn has_text(output: &Value, check: fn(&str) -> bool) -> bool {
    output.as_str().is_some_and(check)
        || output
            .as_array()
            .is_some_and(|parts| parts.iter().any(|p| p["text"].as_str().is_some_and(check)))
        || output["content"]
            .as_array()
            .is_some_and(|parts| parts.iter().any(|p| p["text"].as_str().is_some_and(check)))
}

fn keeper_refusal(text: &str) -> bool {
    let text = text.trim_start().to_ascii_lowercase();
    ["refused:", "forbidden:", "timeout", "timed out", "error:"]
        .iter()
        .any(|prefix| text.starts_with(prefix))
}

fn nonzero_exit(text: &str) -> bool {
    // Only the runner's metadata before its output can establish a process exit.
    text.lines()
        .take_while(|line| !line.starts_with("Output:") && !line.starts_with("Final output:"))
        .find_map(|line| line.strip_prefix("Process exited with code "))
        .and_then(|code| code.trim().parse::<i32>().ok())
        .is_some_and(|code| code != 0)
}
