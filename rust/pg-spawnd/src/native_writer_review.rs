//! Review rounds of the native writer: fast checks with a per-head stamp, then one
//! spawner review run on the exact head, its verdict read from the run record.
use super::{context, output, Host};
use crate::native_writer::Driver;
use pg_worker_core::run::record_io::atomic_write_private;
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
};

/// The diff base of a review card: the previous review's head when the head moved on, else
/// the batch base. A re-review of the same head (after a refuted finding) would otherwise
/// ask for the empty delta head..head.
pub(super) fn review_diff_base<'a>(
    base: &'a str,
    head: &str,
    previous: Option<(&'a str, &str)>,
) -> &'a str {
    previous
        .map(|p| p.0)
        .filter(|prev| *prev != head)
        .unwrap_or(base)
}

/// Native review prompts start with tool instructions, then a separately delimited
/// skill, then the versioned brief. The spawner drops the first two blocks for inline seats.
pub(super) const TOOLS_ONLY_OPEN: &str = "[tools-only]";
pub(super) const TOOLS_ONLY_CLOSE: &str = "[/tools-only]";

/// Oldest-first Git records pair the commit body with its Plan-Item trailer values.
fn skipped_items(brief: &str, log: &str) -> Vec<String> {
    let keys = context::item_keys(brief);
    let mut skipped = BTreeMap::new();
    let mut records = log.split_terminator('\0');
    while let (Some(body), Some(trailers)) = (records.next(), records.next()) {
        for line in body.lines() {
            let Some((key, reason)) = line
                .strip_prefix("SKIPPED ")
                .and_then(|s| s.split_once(' '))
            else {
                continue;
            };
            let reason = reason.trim();
            if keys.binary_search_by(|item| item.as_str().cmp(key)).is_ok()
                && !reason.is_empty()
                && !reason.chars().any(char::is_control)
            {
                skipped.insert(key, format!("SKIPPED {key} {reason}"));
            }
        }
        for key in trailers.lines() {
            skipped.remove(key.trim());
        }
    }
    skipped.into_values().collect()
}

impl Host {
    /// Fast-checks stamp of `head`, in the worktree's own git directory.
    pub(super) fn stamp(&self, head: &str) -> Result<PathBuf, String> {
        if head.len() != 40 || !head.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err("invalid head".into());
        }
        let gitdir = self.git(&["rev-parse", "--absolute-git-dir"])?;
        Ok(Path::new(&gitdir).join("fast-checks").join(head))
    }
    /// The repository's seconds-fast gates present in the checkout: file size, new-code
    /// language, changelog/docs, commit trailers and, with a words file, the private scrub;
    /// the result is their combined output.
    pub(super) fn checks(&self) -> Result<String, String> {
        let mut log = String::new();
        let pin = self.base_pin();
        let range = |extra: &[&str]| {
            let mut args: Vec<String> = extra.iter().map(|s| s.to_string()).collect();
            args.extend(["--base".into(), pin.clone(), "--head".into(), "HEAD".into()]);
            args
        };
        let mut gates: Vec<(&str, Vec<String>)> = vec![
            ("check_file_size.py", vec![]),
            ("check_new_code_lang.py", vec![]),
            ("check_changelog_docs.py", vec![]),
            ("ci_commit_gate.py", range(&["check"])),
        ];
        let words = std::env::var_os("HOME")
            .map(|home| Path::new(&home).join(".config/plangraph/scrub-words.txt"))
            .filter(|path| path.is_file());
        if let Some(words) = words {
            let mut args = range(&[]);
            args.extend(["--words".into(), words.to_string_lossy().into_owned()]);
            gates.push(("preflight_private_scrub.py", args));
        }
        for (gate, args) in gates {
            let path = self.cwd.join("tools").join(gate);
            if !path.is_file() {
                continue;
            }
            let mut command = self.command(if cfg!(windows) { "python" } else { "python3" });
            command.arg(path).args(args).current_dir(&self.cwd);
            // Both streams of a passing gate are evidence too (a skipped check is reported
            // on stderr).
            let result = command.output().map_err(|e| format!("{gate}: {e}"))?;
            let both = format!(
                "{}{}",
                String::from_utf8_lossy(&result.stdout),
                String::from_utf8_lossy(&result.stderr)
            );
            if !result.status.success() {
                return Err(format!(
                    "{gate}: command failed ({}):\nstdout: {}\nstderr: {}",
                    result.status,
                    String::from_utf8_lossy(&result.stdout).trim(),
                    String::from_utf8_lossy(&result.stderr).trim()
                ));
            }
            log.push_str(&format!("== {gate} ({})\n{both}\n", result.status));
        }
        Ok(log)
    }
    /// Item-context evidence of the versioned review brief, from the writer brief and the
    /// exact diff: item keys, whole done criteria, invariants, both SHAs, the public symbols
    /// the diff adds with their first callers, and the exact-head check and session logs.
    fn brief_context(&self, base: &str, head: &str) -> Value {
        let mut context = json!({"base_sha": base, "head_sha": head});
        let keys = context::item_keys(&self.brief);
        if !keys.is_empty() {
            context["item_key"] = json!(keys.join(", "));
        }
        if let Some(done) = context::done_criteria(&self.brief) {
            context["done_criteria"] = json!(done);
        }
        if let Some(invariants) = context::invariants(&self.brief) {
            context["invariants"] = json!(invariants);
        }
        let diff = self.git(&["diff", "-U0", base, head]).unwrap_or_default();
        let symbols = context::changed_symbols(&diff, &|path| {
            self.git(&["show", &format!("{head}:{path}")]).ok()
        });
        if !symbols.is_empty() {
            let names: Vec<&str> = symbols.iter().map(|s| s.name.as_str()).collect();
            context["public_symbols"] = json!(names.join(", "));
            let callers: Vec<String> = symbols
                .iter()
                .filter_map(|symbol| {
                    let grep = self
                        .git(&["grep", "-n", "-w", "--", &symbol.name, "--", "*.rs", "*.py"])
                        .unwrap_or_default();
                    context::callers_line(symbol, &grep)
                })
                .collect();
            if !callers.is_empty() {
                context["callers"] = json!(callers.join("\n"));
            }
        }
        let mut logs = Vec::new();
        if let Ok(stamp) = self.stamp(head) {
            logs.push(format!(
                "fast checks (exact head): {}",
                stamp.with_extension("log").display()
            ));
        }
        if let Ok(stamp) = self.stamp(head) {
            let excerpt = stamp.with_extension("session.log");
            if excerpt.is_file() {
                logs.push(format!(
                    "writer session of the turns that produced this head, with their cargo runs: {}",
                    excerpt.display()
                ));
            }
        }
        if !logs.is_empty() {
            context["test_output_paths"] = json!(logs.join("; "));
        }
        context
    }
    /// Copies the part of the writer session log written since the previous stamped head to
    /// `<stamp>.session.log`: the turns, with their cargo runs, that produced this head. No
    /// session log, no excerpt.
    pub(super) fn session_excerpt(&self, stamp: &Path) -> Result<(), String> {
        let Some(session) = self.record_path.parent().map(|d| d.join("native.log")) else {
            return Ok(());
        };
        let Ok(data) = fs::read(&session) else {
            return Ok(());
        };
        let marker = stamp.with_file_name("session-offset");
        let from: usize = fs::read_to_string(&marker)
            .ok()
            .and_then(|text| text.trim().parse().ok())
            .unwrap_or(0);
        fs::write(
            stamp.with_extension("session.log"),
            &data[from.min(data.len())..],
        )
        .map_err(|e| e.to_string())?;
        fs::write(&marker, data.len().to_string()).map_err(|e| e.to_string())
    }
    /// Review prompt for `head`: review skill, writer brief, diff range (the delta from the
    /// previous review's head when it is an ancestor) and the required closing lines.
    pub(super) fn review_prompt(
        &mut self,
        base: &str,
        head: &str,
        previous: Option<(&str, &str)>,
        families: &[String],
        versioned: &str,
        skill: &str,
    ) -> Result<String, String> {
        let diff_base = review_diff_base(base, head, previous);
        let skill_block = if skill.is_empty() {
            String::new()
        } else {
            format!("[review-skill]\n{skill}\n[/review-skill]\n\n")
        };
        let mut prompt = format!("{TOOLS_ONLY_OPEN}Your working directory is a disposable checkout of the head under review (no push). Open callers, tests and docs to verify each finding; run read-only commands and the tests a finding depends on; never edit. Use the inherited shared CARGO_TARGET_DIR. To read the review diff, use git diff {diff_base} {head}.{TOOLS_ONLY_CLOSE}\n\n{skill_block}{versioned}\n\nWriter brief (item keys and Done-when lines):\n{}\n\nWriter families of {base}..{head} (every one is excluded from this review): {}.\n", self.brief, families.join(", "));
        let messages = self.git(&[
            "log",
            "--reverse",
            "--topo-order",
            "-z",
            "--format=%B%x00%(trailers:key=Plan-Item,valueonly,unfold)",
            &format!("{base}..{head}"),
        ])?;
        let skipped = skipped_items(&self.brief, &messages);
        if !skipped.is_empty() {
            prompt.push_str("Writer-reported skipped items from the committed range:\n");
            prompt.push_str(&skipped.join("\n"));
            prompt.push_str("\n\n");
        }
        match previous {
            Some((prev, text)) if prev == head => prompt.push_str(&format!("This is a later round on the same head {head}: the previous review (its text follows) judged it and the writer answered its findings with evidence. Review the whole change {base}..{head} and check that every earlier HIGH and MED finding is fixed or refuted with evidence.\n----- previous review -----\n{text}\n----- end of previous review -----\n")),
            Some((prev, text)) => prompt.push_str(&format!("This is a later round: the previous review judged head {prev} (its text follows). Review the delta {prev}..{head}, check that every earlier HIGH and MED finding is fixed or refuted with evidence, and judge the whole change {base}..{head} where the delta touches it.\n----- previous review -----\n{text}\n----- end of previous review -----\n")),
            None => prompt.push_str(&format!("The diff is base {base}..head {head}.\n")),
        }
        prompt.push_str(&format!("Name each finding as file:line. Include one TEST-GAMING: line using the seat's required format. List the repository files you opened in a FILES READ: line. End with the line HEAD: {head} and then the line VERDICT: ACCEPT or VERDICT: REQUEST_CHANGES.\n"));
        Ok(prompt)
    }
    /// One review run through the spawner, role review with every writer family of the range excluded,
    /// in a disposable no-push clone at `head`; the verdict comes from that run's record.
    pub(super) fn review_run(
        &mut self,
        head: &str,
        previous: Option<&Value>,
    ) -> Result<Value, String> {
        let base = self.git(&["rev-parse", &format!("{}^{{commit}}", self.base_pin())])?;
        self.reviews_started += 1;
        let tag = format!("{}-{}", &head[..8.min(head.len())], self.reviews_started);
        fs::create_dir_all(&self.reviews).map_err(|e| e.to_string())?;
        let clone = self.reviews.join(format!("checkout-{tag}"));
        let _ = fs::remove_dir_all(&clone);
        let origin = self.git(&["remote", "get-url", "origin"])?;
        output(
            self.command("git")
                .args(["clone", "-q", "--shared", "--no-checkout"])
                .arg(&self.cwd)
                .arg(&clone),
        )?;
        let result = self.review_in(&clone, &tag, &origin, &base, head, previous);
        let _ = fs::remove_dir_all(&clone);
        result
    }
    fn review_in(
        &mut self,
        clone: &Path,
        tag: &str,
        origin: &str,
        base: &str,
        head: &str,
        previous: Option<&Value>,
    ) -> Result<Value, String> {
        let git = |host: &Self, args: &[&str]| {
            output(host.command("git").arg("-C").arg(clone).args(args))
        };
        git(self, &["checkout", "-q", "--detach", head])?;
        git(self, &["remote", "set-url", "origin", origin])?;
        git(
            self,
            &["remote", "set-url", "--push", "origin", "/dev/null/no-push"],
        )?;
        git(self, &["update-ref", "refs/review/base", base])?;
        let prev = previous
            .and_then(|review| Some((review["head"].as_str()?, review["text"].as_str()?)))
            .filter(|(prev, _)| {
                self.git(&["merge-base", "--is-ancestor", prev, head])
                    .is_ok()
            });
        let skill = self.skill("review").unwrap_or_default();
        let trusted_python = || {
            let mut python =
                pg_worker_core::writer_families::python_command(self.python_src.as_deref());
            python.env("PATH", &self.tool_path);
            python
        };
        // Every writer family of base..head (trailers, plus the served writer model) is excluded.
        let families = pg_worker_core::writer_families::writer_families(
            &self.cwd,
            base,
            head,
            &[super::WRITER_MODEL],
            &trusted_python,
        )?;
        let card = json!({"estimate_tokens":300000, "max_min":60, "stall_min":20,
            "repository_review_required":true,
            "diff_ref":{"repo":self.repo, "base":review_diff_base(base, head, prev), "head":head}});
        if self.review_override.is_none() {
            if let Some(deferred) = self.review_preflight(clone, &card, &families, head)? {
                return Ok(deferred);
            }
        }
        // Fixed prefix, the review skill as rules and the item context of the versioned
        // review brief, from the one renderer; the context carries the actual evidence.
        let context = self.brief_context(base, head);
        let versioned =
            pg_worker_core::writer_families::render_review_brief(&context, "", &trusted_python)?;
        let prompt = self.review_prompt(base, head, prev, &families, &versioned, &skill)?;
        let prompt_path = self.reviews.join(format!("review-{tag}.prompt.txt"));
        atomic_write_private(&prompt_path, prompt.as_bytes()).map_err(|e| e.to_string())?;
        let serialized_card = card.to_string();
        let mut command = self.command(&self.review_cli[0]);
        command
            .args(&self.review_cli[1..])
            .arg("--state")
            .arg(&self.state)
            .args(["spawn", "--card", &serialized_card, "--role", "review"])
            .args(families.iter().flat_map(|f| ["--exclude-family", f]));
        if let Some((leg, reason)) = &self.review_override {
            command.args([
                "--leg",
                leg,
                "--override-reason",
                reason,
                "--no-fallthrough",
            ]);
        }
        let printed = command
            .arg("--cwd")
            .arg(clone)
            .arg("--prompt-file")
            .arg(&prompt_path)
            .args(["--wait", "3300", "--json"])
            .current_dir(clone)
            .output()
            .map_err(|e| e.to_string())?;
        let record =
            printed_record(&String::from_utf8_lossy(&printed.stdout)).ok_or_else(|| {
                format!(
                    "review run returned no record: {}",
                    String::from_utf8_lossy(&printed.stderr).trim()
                )
            })?;
        let chosen = &record["chosen_leg"];
        if let Some((leg, _)) = &self.review_override {
            if chosen["id"] != *leg {
                return Err(format!("review leg differs from requested leg {leg}"));
            }
        }
        if record["status"] != "completed" {
            return Err(format!("review run incomplete: {}", record["reason"]));
        }
        let mut text = review_run_text(&self.state, &record)?;
        if crate::review_test_evidence::tests_line(&text).is_none_or(str::is_empty) {
            text.insert_str(0, "TESTS: none: reviewer supplied no test result\n");
        }
        let paths = crate::review_citation::diff_paths(&self.cwd, Some(&self.base_pin()), head);
        let mut verdict = review_verdict(&text, head, &paths);
        let id = record["run_id"].as_str().unwrap_or("run-unknown");
        let path = self.reviews.join(format!("{tag}-{id}.txt"));
        atomic_write_private(&path, text.as_bytes()).map_err(|e| e.to_string())?;
        verdict["run_id"] = json!(id);
        verdict["path"] = json!(path);
        verdict["leg"] = chosen["id"].clone();
        verdict["harness"] = chosen["harness"].clone();
        if verdict["verdict"] == "ACCEPT" {
            verdict["accepted_at"] = record["ended"].as_f64().map_or_else(
                || json!(chrono::Utc::now().to_rfc3339()),
                |ended| json!(ended),
            );
        }
        Ok(verdict)
    }
}
/// Structured verdict of one review text bound to `head`: the last VERDICT line decides and
/// must be canonical; a `HEAD: <sha>` line naming another head, or an ACCEPT that names no
/// changed file, is `no_verdict`.
pub(crate) fn review_verdict(text: &str, head: &str, paths: &[String]) -> Value {
    let weak = |reason: &str| json!({"verdict":"no_verdict","status":"no_verdict","reason":reason,"head":head,"text":text});
    let other_head = text.lines().any(|line| {
        line.trim()
            .strip_prefix("HEAD: ")
            .is_some_and(|sha| sha.len() == 40 && sha != head)
    });
    if other_head {
        return weak("review names another head");
    }
    let Some(line) = text
        .lines()
        .rev()
        .find(|l| l.to_ascii_uppercase().contains("VERDICT"))
    else {
        return weak("no verdict line");
    };
    let verdict = match line.trim() {
        "VERDICT: ACCEPT" => "ACCEPT",
        "VERDICT: REQUEST_CHANGES" | "VERDICT: REJECT" => "REQUEST_CHANGES",
        _ => return weak("non-canonical verdict line"),
    };
    if verdict == "ACCEPT" && !crate::review_artifacts::has_repository_evidence(text, paths) {
        return weak("weak_verdict");
    }
    let tested = crate::review_test_evidence::reported(text).is_some();
    json!({"verdict":verdict,"head":head,"text":text,"tested":tested})
}
/// Spawner CLI argv prefix for review runs; PLANGRAPH_NATIVE_REVIEW_CLI (a JSON string
/// array) replaces the default `python3 -m plangraph.spawner.cli`.
pub(super) fn review_cli() -> Result<Vec<String>, String> {
    match std::env::var("PLANGRAPH_NATIVE_REVIEW_CLI") {
        Ok(text) => serde_json::from_str::<Vec<String>>(&text)
            .ok()
            .filter(|argv| !argv.is_empty())
            .ok_or_else(|| {
                "PLANGRAPH_NATIVE_REVIEW_CLI must be a non-empty JSON string array".into()
            }),
        Err(_) => Ok(vec![
            if cfg!(windows) { "python" } else { "python3" }.into(),
            "-m".into(),
            "plangraph.spawner.cli".into(),
        ]),
    }
}
/// The run record a spawner CLI printed: the whole output, or its last top-level object.
pub(super) fn printed_record(stdout: &str) -> Option<Value> {
    serde_json::from_str::<Value>(stdout.trim())
        .ok()
        .or_else(|| {
            let start = stdout.rfind("\n{")? + 1;
            serde_json::from_str::<Value>(stdout[start..].trim()).ok()
        })
        .filter(|record| record["run_id"].is_string())
}
/// Review text of a review run record: the rendered result of its live log, else the
/// record's `stdout_tail` (OpenCode legs keep their answer text there).
pub(super) fn review_run_text(state: &Path, record: &Value) -> Result<String, String> {
    if let Some(error) = record["review_binding_error"].as_str() {
        return Err(error.to_owned());
    }
    let rendered = crate::review_artifacts::review_text(state, record)
        .map_err(|_| "review run record unreadable".to_owned())?;
    let canonical = |text: &str| {
        text.lines().any(|line| {
            matches!(
                line.trim(),
                "VERDICT: ACCEPT" | "VERDICT: REQUEST_CHANGES" | "VERDICT: REJECT"
            )
        })
    };
    match record["stdout_tail"].as_str() {
        Some(tail) if !canonical(&rendered) && canonical(tail) => Ok(tail.to_owned()),
        _ => Ok(rendered),
    }
}
