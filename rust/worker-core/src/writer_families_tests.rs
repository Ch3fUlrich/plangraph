use super::*;
use crate::git_test_support::command;

fn git(repo: &Path, args: &[&str]) -> String {
    let out = command(repo).args(args).output().unwrap();
    assert!(out.status.success(), "{args:?}: {out:?}");
    String::from_utf8_lossy(&out.stdout).trim().to_owned()
}
fn commit(repo: &Path, name: &str, message: &str) {
    std::fs::write(repo.join(name), message).unwrap();
    git(repo, &["add", name]);
    git(
        repo,
        &[
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-q",
            "-m",
            message,
        ],
    );
}
fn python() -> Command {
    // The checkout holding src/plangraph is two levels above this crate.
    python_command(Some(&Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")))
}

#[test]
fn every_writer_family_of_the_range_comes_from_its_trailers() {
    let dir = tempfile::tempdir().unwrap();
    let repo = dir.path();
    git(repo, &["init", "-q", "-b", "main"]);
    commit(repo, "base.txt", "base");
    let base = git(repo, &["rev-parse", "HEAD"]);
    commit(
        repo,
        "a.txt",
        "a\n\nWriter: gemini-4-hydrogen-high run-6f00f4b3",
    );
    commit(
        repo,
        "b.txt",
        "b\n\nWriter: gpt-6.2-sol-medium run-0ad2ce51",
    );
    commit(repo, "c.txt", "c\n\nHand-Edit: changelog");
    let head = git(repo, &["rev-parse", "HEAD"]);
    let families = writer_families(repo, &base, &head, &[], &python).unwrap();
    assert_eq!(families, ["google", "openai"]);
    // A serving model that wrote without a trailer joins the set.
    let families = writer_families(repo, &base, &head, &["claude-sonnet-5-5"], &python).unwrap();
    assert_eq!(families, ["anthropic", "google", "openai"]);
    // A range whose commits name no family is decided by the leg models alone.
    commit(repo, "d.txt", "d");
    let head = git(repo, &["rev-parse", "HEAD"]);
    let prior = git(repo, &["rev-parse", "HEAD~1"]);
    assert!(writer_families(repo, &prior, &head, &[], &python).is_err());
    assert_eq!(
        writer_families(repo, &prior, &head, &["gpt-6.2-sol"], &python).unwrap(),
        ["openai"]
    );
    assert!(
        writer_families(repo, &prior, &head, &["no-such-model"], &python)
            .unwrap_err()
            .contains("unknown writer family of model no-such-model")
    );
}

#[test]
fn a_merge_commits_writer_trailer_counts_next_to_ordinary_commits() {
    let dir = tempfile::tempdir().unwrap();
    let repo = dir.path();
    git(repo, &["init", "-q", "-b", "main"]);
    commit(repo, "base.txt", "base");
    let base = git(repo, &["rev-parse", "HEAD"]);
    git(repo, &["checkout", "-q", "-b", "side"]);
    commit(repo, "side.txt", "side");
    git(repo, &["checkout", "-q", "main"]);
    commit(
        repo,
        "a.txt",
        "a\n\nWriter: gpt-6.2-sol-medium run-0ad2ce51",
    );
    git(
        repo,
        &[
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "merge",
            "-q",
            "--no-ff",
            "side",
            "-m",
            "merge side\n\nWriter: gemini-4-hydrogen-high run-6f00f4b3",
        ],
    );
    let head = git(repo, &["rev-parse", "HEAD"]);
    // The merge brings in its side commit (no trailer, unknown author) and carries a trailer itself.
    let families = writer_families(repo, &base, &head, &[], &python).unwrap();
    assert_eq!(families, ["google", "openai"]);
    // Per commit: the merge's own trailer is the merge's, the ordinary commit keeps its writer, and the
    // side commit (no trailer, unknown author) resolves to nothing.
    let read = commit_families(repo, &base, &head, &[], &python).unwrap();
    let set = |names: &[&str]| {
        names
            .iter()
            .map(|n| n.to_string())
            .collect::<BTreeSet<String>>()
    };
    let by_subject = |subject: &str| {
        let sha = git(
            repo,
            &[
                "log",
                "-1",
                "--format=%H",
                &format!("--grep={subject}"),
                &head,
            ],
        );
        read.per_commit
            .iter()
            .find(|(s, _)| *s == sha)
            .unwrap()
            .1
            .clone()
    };
    assert_eq!(by_subject("merge side"), set(&["google"]));
    assert_eq!(by_subject("^a$"), set(&["openai"]));
    assert_eq!(by_subject("^side$"), set(&[]));
    assert_eq!(read.per_commit.len(), 3);
}

#[test]
fn a_range_where_nothing_resolves_is_refused_once_for_every_caller() {
    let dir = tempfile::tempdir().unwrap();
    let repo = dir.path();
    git(repo, &["init", "-q", "-b", "main"]);
    commit(repo, "base.txt", "base");
    let base = git(repo, &["rev-parse", "HEAD"]);
    commit(repo, "x.txt", "x");
    let head = git(repo, &["rev-parse", "HEAD"]);
    for error in [
        commit_families(repo, &base, &head, &[], &python)
            .err()
            .unwrap(),
        writer_families(repo, &base, &head, &[], &python).unwrap_err(),
    ] {
        assert!(
            error.starts_with("refused: could not determine any writer family"),
            "{error}"
        );
    }
}

#[test]
fn keeper_families_fold_spellings_into_the_keeper_table() {
    let names: Vec<String> = ["claude", "anthropic", "gpt", "openai", "deepseek"]
        .iter()
        .map(|s| s.to_string())
        .collect();
    let table = keeper_families(&names, &python).unwrap();
    assert_eq!(table["claude"], table["anthropic"]);
    assert_eq!(table["gpt"], table["openai"]);
    assert_ne!(table["claude"], table["gpt"]);
    assert_eq!(table["deepseek"], "deepseek");
}

#[test]
fn a_lane_head_review_brief_refuses_empty_item_context_by_field_name() {
    let full = serde_json::json!({
        "item_key": "demo:CTX", "public_symbols": "run", "callers": "none (verified empty: no caller)",
        "test_output_paths": "logs/fast.log"});
    let brief = render_review_brief(&full, "", &python).unwrap();
    assert!(brief.contains("Callers of changed public symbols: none (verified empty: no caller)"));
    for field in ["public_symbols", "callers", "test_output_paths"] {
        let mut context = full.clone();
        context.as_object_mut().unwrap().remove(field);
        let refusal = render_review_brief(&context, "", &python).unwrap_err();
        assert!(refusal.contains(field), "{field}: {refusal}");
    }
}

#[test]
fn classify_review_uses_the_one_classifier_with_the_serving_attempt() {
    use sha2::{Digest, Sha256};
    let context = "=== ITEM CONTEXT\nItem key: k\nChanged public symbols: run\nCallers of changed public symbols: \nAffected test output (paths): l.log\n";
    let attempt = |prompt: &str, digest: Option<&str>| {
        let sha = format!("{:x}", Sha256::digest(prompt.as_bytes()));
        serde_json::json!({"prompt": prompt, "prompt_sha256": digest.unwrap_or(&sha), "binding": null, "cwd": null})
    };
    let prompt = format!("{context}diff --git a/src/a.rs b/src/a.rs\n");
    let text =
        "PROCESS review_brief.md field=callers - no callers rendered\nVERDICT: REQUEST_CHANGES\n";
    let held = classify_review(text, &attempt(&prompt, None), &python).unwrap();
    assert_eq!((held.blocking, held.process.len()), (0, 1));
    // The dispatched diff changes the brief itself: a finding of the head.
    let touched = format!("{prompt}diff --git a/src/plangraph/review/review_brief.md b/src/plangraph/review/review_brief.md\n");
    assert_eq!(
        classify_review(text, &attempt(&touched, None), &python)
            .unwrap()
            .blocking,
        1
    );
    // The prompt renders the field, or is not the one the attempt recorded: a finding of the head.
    let rendered = prompt.replace(
        "symbols: run\nCallers of changed public symbols: \n",
        "symbols: run\nCallers of changed public symbols: a:1\n",
    );
    assert_eq!(
        classify_review(text, &attempt(&rendered, None), &python)
            .unwrap()
            .blocking,
        1
    );
    assert_eq!(
        classify_review(text, &attempt(&prompt, Some(&"0".repeat(64))), &python)
            .unwrap()
            .blocking,
        1
    );
    // An untyped MED about the brief is never a diagnostic.
    let med = "MED review_brief.md:26 - omits the callers\nVERDICT: REQUEST_CHANGES\n";
    let counted = classify_review(med, &attempt(&prompt, None), &python).unwrap();
    assert_eq!((counted.blocking, counted.process.len()), (1, 0));
}
