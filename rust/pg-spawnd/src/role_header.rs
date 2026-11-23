//! Role header and role skill of a writer prompt; the header words come from the file the
//! Python spawner reads too (src/plangraph/config/role_headers.json).
use serde_json::Value;

const TABLE: &str = include_str!("../../../src/plangraph/config/role_headers.json");
/// Instructions for writer turns managed by the native driver.
pub const NATIVE_RULES: &str = concat!(
    "Native writer rules: new code is Rust with Rust tests. ",
    "No new Python, shell or Node source files or additions to tools/new_code_allowlist.txt without an operator D-id; fixes inside existing Python files remain allowed until their port. ",
    "Do not push. Commit your work and end your turn: the driver runs the fast checks and an other-family review on your final HEAD and resumes this session with any failure or finding.\n",
    "Use the inherited shared CARGO_TARGET_DIR for every Cargo command, including reviews and preflight. Never override it with a private /tmp or worktree target, --target-dir, or Cargo configuration.",
);

pub(crate) fn header(agent: &str, role: &str) -> String {
    let table: Value = serde_json::from_str(TABLE).expect("role_headers.json is valid");
    let name = table["agents"][agent]
        .as_str()
        .map_or_else(|| format!("the {agent} agent"), str::to_owned);
    table["roles"][role]
        .as_str()
        .expect("role header exists")
        .replace("{agent}", &name)
}
pub(crate) fn is_batch(brief: &str) -> bool {
    brief.contains("\n# Items\n") && brief.contains("\n# Run values")
}
pub fn skill_name(brief: &str) -> &'static str {
    if is_batch(brief) {
        "batch-writer"
    } else {
        "implement"
    }
}
/// Render the header and byte-identical skill, with driver rules for native Codex.
pub fn compose(agent: &str, brief: &str, skill: Option<&str>) -> String {
    let head = header(agent, "implement");
    let body = brief
        .strip_prefix(head.as_str())
        .map_or(brief, |rest| rest.trim_start_matches('\n'));
    let (skill_block, body) = match skill {
        Some(text) => (
            format!("{text}\n"),
            body.strip_prefix(text)
                .map_or(body, |rest| rest.trim_start_matches('\n')),
        ),
        None => (String::new(), body),
    };
    let rules_block = if agent == "codex" {
        format!("{NATIVE_RULES}\n\n")
    } else {
        String::new()
    };
    let body = body.strip_prefix(&rules_block).unwrap_or(body);
    format!("{head}\n\n{skill_block}{rules_block}{body}")
}
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn header_names_codex_without_autoos() {
        let text = header("codex", "implement");
        assert!(text.contains("Codex") && !text.contains("AutoOS") && !text.contains("opencode"));
        assert!(header("codex", "review").contains("read-only"));
    }
    #[test]
    fn skill_comes_once_after_header() {
        let skill = "---\nname: batch-writer\n---\nbody\n";
        let brief = "x\n# Items\na\n\n# Run values\nNAME: n\n";
        assert_eq!(skill_name(brief), "batch-writer");
        let out = compose("codex", brief, Some(skill));
        assert!(out.starts_with(&header("codex", "implement")));
        assert!(out.contains(&format!("\n\n{skill}\n{NATIVE_RULES}\n\nx\n")));
        assert_eq!(compose("codex", &out, Some(skill)), out);
        let own = format!("{skill}\n{brief}");
        assert_eq!(
            compose("codex", &own, Some(skill)).matches(skill).count(),
            1
        );
    }
    #[test]
    fn prompts_of_two_briefs_share_header_and_skill() {
        let skill = "S\n";
        let a = compose("codex", "A", Some(skill));
        let b = compose("codex", "B", Some(skill));
        let prefix = format!(
            "{}\n\n{skill}\n{NATIVE_RULES}\n\n",
            header("codex", "implement")
        );
        assert!(a.starts_with(&prefix) && b.starts_with(&prefix));
        assert_eq!(skill_name("plain"), "implement");
    }

    #[test]
    fn fleet_batch_prompt_leaves_review_to_the_batch_writer() {
        let skill = "batch writer skill\n";
        let brief = "# Items\n\n# Run values\n";
        assert_eq!(
            compose("claude", brief, Some(skill)),
            format!("{}\n\n{skill}\n{brief}", header("claude", "implement"))
        );
    }
}
