//! Parse settings permission arrays without interpreting or echoing rule arguments.
use serde::{Deserialize, Deserializer};

#[derive(Deserialize)]
struct Settings {
    #[serde(default, rename = "permissions")]
    _permissions: Option<Permissions>,
}
#[derive(Deserialize)]
struct Permissions {
    #[serde(default, rename = "allow")]
    _allow: Vec<Rule>,
    #[serde(default, rename = "deny")]
    _deny: Vec<Rule>,
}
struct Rule;
impl<'de> Deserialize<'de> for Rule {
    fn deserialize<D: Deserializer<'de>>(de: D) -> Result<Self, D::Error> {
        struct RuleVisitor;
        impl serde::de::Visitor<'_> for RuleVisitor {
            type Value = Rule;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("a permission rule")
            }
            fn visit_str<E: serde::de::Error>(self, rule: &str) -> Result<Rule, E> {
                if rule.trim_start().starts_with("mcp__") && rule.contains('(') {
                    return Err(E::custom("MCP permission rule contains parentheses"));
                }
                Ok(Rule)
            }
        }
        de.deserialize_str(RuleVisitor)
    }
}
pub fn check(text: &str) -> Result<(), serde_json::Error> {
    serde_json::from_str::<Settings>(text).map(|_| ())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn mcp_parentheses_refuse_with_original_line() {
        let text = "{\n  \"permissions\": {\n    \"allow\": [\n      \"mcp__terminal__run(command:*)\"\n    ]\n  }\n}";
        let error = check(text).unwrap_err();
        assert_eq!(error.line(), 4);
        assert!(error.to_string().contains("MCP"));
        assert!(check(r#"{"permissions":{"allow":["mcp__terminal__run","Bash(git:*)"],"deny":["mcp__shell__run"]}}"#).is_ok());
        assert!(check(r#"{"permissions":{"deny":["mcp__shell__run(*)"]}}"#).is_err());
        assert!(check(r#"{"permissions":{"allow":["mcp__shell__run\u0028*\u0029"]}}"#).is_err());
    }
}
