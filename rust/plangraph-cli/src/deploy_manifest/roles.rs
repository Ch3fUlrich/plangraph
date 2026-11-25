//! The host roles of this machine: where they are read from and how a typo is refused.
use super::ROLES;
use std::collections::BTreeSet;
use std::path::Path;

/// The roles of this host: `PG_HOST_ROLES` (comma or space separated), else the words of
/// `<config>/plangraph/host-roles`. Unknown words are an error, so a typo never silently drops a role.
pub fn host_roles(
    env_value: Option<&str>,
    file_text: Option<&str>,
) -> Result<BTreeSet<String>, String> {
    let text = match (env_value.filter(|v| !v.trim().is_empty()), file_text) {
        (Some(v), _) => v,
        (None, Some(f)) => f,
        (None, None) => "",
    };
    let mut roles = BTreeSet::new();
    for word in text
        .split(|c: char| c == ',' || c.is_whitespace())
        .filter(|w| !w.is_empty())
    {
        if !ROLES.contains(&word) {
            return Err(format!(
                "unknown host role {word:?} (roles: {})",
                ROLES.join(", ")
            ));
        }
        roles.insert(word.to_owned());
    }
    Ok(roles)
}

/// The roles of the host that runs this process; `config_dir` is `~/.config/plangraph`.
pub fn this_host_roles(config_dir: &Path) -> Result<BTreeSet<String>, String> {
    let file = std::fs::read_to_string(config_dir.join("host-roles")).ok();
    host_roles(
        std::env::var("PG_HOST_ROLES").ok().as_deref(),
        file.as_deref(),
    )
}
