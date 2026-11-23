//! Role header and role skill of a writer prompt; the header words come from the file the
//! Python spawner reads too (src/plangraph/config/role_headers.json).
use serde_json::Value;

const TABLE: &str = include_str!("../../../src/plangraph/config/role_headers.json");

/// Render the configured header for an agent and role.
pub fn header(agent: &str, role: &str) -> String {
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
/// Header, then the skill byte-identical unless the brief already starts with it, then the brief.
pub fn compose(agent: &str, brief: &str, skill: Option<&str>) -> String {
    let head = header(agent, "implement");
    let body = brief
        .strip_prefix(head.as_str())
        .map_or(brief, |rest| rest.trim_start_matches('\n'));
    match skill {
        Some(text) if !body.starts_with(text) => format!("{head}\n\n{text}\n{body}"),
        _ => format!("{head}\n\n{body}"),
    }
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
        assert!(out.contains(&format!("\n\n{skill}\nx\n")));
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
        let prefix = format!("{}\n\n{skill}\n", header("codex", "implement"));
        assert!(a.starts_with(&prefix) && b.starts_with(&prefix));
        assert_eq!(skill_name("plain"), "implement");
    }
}
