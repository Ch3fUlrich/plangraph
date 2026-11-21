//! Review rounds of the native writer: fast checks with a per-head stamp, then one
//! spawner review run on the exact head, its verdict read from the run record.
use super::{output, Host};
use crate::native_writer::Driver;
use pg_worker_core::run::record_io::atomic_write_private;
use serde_json::{json, Value};
use std::{
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

/// The sentences of a review prompt that need file and shell tools form one block at the very start of the prompt, between these markers (the skill and the
/// writer brief follow it, so brief text can never be mistaken for a marker); the spawner
/// (prompt_input.compose) drops the block, and the skill, for a toolless inline seat.
pub(super) const TOOLS_ONLY_OPEN: &str = "[tools-only]";
pub(super) const TOOLS_ONLY_CLOSE: &str = "[/tools-only]";

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
    /// language, changelog/docs, commit trailers and, with a words file, the private scrub.
    pub(super) fn checks(&self) -> Result<(), String> {
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
            output(
                self.command(if cfg!(windows) { "python" } else { "python3" })
                    .arg(path)
                    .args(args)
                    .current_dir(&self.cwd),
            )
            .map_err(|e| format!("{gate}: {e}"))?;
        }
        Ok(())
    }
    /// Review prompt for `head`: review skill, writer brief, diff range (the delta from the
    /// previous review's head when it is an ancestor) and the required closing lines.
    pub(super) fn review_prompt(
        &mut self,
        base: &str,
        head: &str,
        previous: Option<(&str, &str)>,
    ) -> String {
        let skill = self.skill("review").unwrap_or_default();
        let mut prompt = format!("{TOOLS_ONLY_OPEN}Your working directory is a disposable checkout at exactly head {head} (no push). Open callers, tests and docs to verify each finding; run read-only commands and the tests a finding depends on; never edit.{TOOLS_ONLY_CLOSE}\n\n{skill}\n\nWriter brief (item keys and Done-when lines):\n{}\n\n", self.brief);
        match previous {
            Some((prev, text)) if prev == head => prompt.push_str(&format!("This is a later round on the same head {head}: the previous review (its text follows) judged it and the writer answered its findings with evidence. Review the whole change {base}..{head} (git diff {base} {head}) and check that every earlier HIGH and MED finding is fixed or refuted with evidence.\n----- previous review -----\n{text}\n----- end of previous review -----\n")),
            Some((prev, text)) => prompt.push_str(&format!("This is a later round: the previous review judged head {prev} (its text follows). Review the delta {prev}..{head} (git diff {prev} {head}), check that every earlier HIGH and MED finding is fixed or refuted with evidence, and judge the whole change {base}..{head} where the delta touches it.\n----- previous review -----\n{text}\n----- end of previous review -----\n")),
            None => prompt.push_str(&format!("The diff is base {base}..head {head} (git diff {base} {head}).\n")),
        }
        prompt.push_str(&format!("Name each finding as file:line. End with the line HEAD: {head} and then the line VERDICT: ACCEPT or VERDICT: REQUEST_CHANGES.\n"));
        prompt
    }
    /// One review run through the spawner, role review with the writer family excluded,
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
        let prompt = self.review_prompt(base, head, prev);
        let prompt_path = self.reviews.join(format!("review-{tag}.prompt.txt"));
        atomic_write_private(&prompt_path, prompt.as_bytes()).map_err(|e| e.to_string())?;
        let card = json!({"cwd":clone, "estimate_tokens":300000, "max_min":60, "stall_min":20,
            "diff_ref":{"repo":clone, "base":review_diff_base(base, head, prev), "head":head}});
        let printed = self
            .command(&self.review_cli[0])
            .args(&self.review_cli[1..])
            .arg("--state")
            .arg(&self.state)
            .args(["spawn", "--card", &card.to_string(), "--role", "review"])
            .args(["--writer-family", "openai", "--prompt-file"])
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
        let text = review_run_text(&self.state, &record)?;
        let paths = crate::review_citation::diff_paths(&self.cwd, Some(&self.base_pin()), head);
        let mut verdict = review_verdict(&text, head, &paths);
        let id = record["run_id"].as_str().unwrap_or("run-unknown");
        let path = self.reviews.join(format!("{tag}-{id}.txt"));
        atomic_write_private(&path, text.as_bytes()).map_err(|e| e.to_string())?;
        verdict["run_id"] = json!(id);
        verdict["path"] = json!(path);
        verdict["leg"] = record["chosen_leg"]["id"].clone();
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
    if verdict == "ACCEPT" && !crate::review_citation::names_file(text, paths) {
        return weak("weak_verdict");
    }
    json!({"verdict":verdict,"head":head,"text":text})
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
