//! Strict TSV rule scopes; see docs/rule-scopes.md for the header contract.
use regex::Regex;
use std::collections::{BTreeMap, BTreeSet};

pub type Tags = BTreeSet<String>;

#[derive(Debug, PartialEq, Eq)]
pub enum ScopeError {
    InvalidHeader(usize),
    InvalidRow(usize),
    UnknownRole(usize),
    UnknownTag(usize),
    TooManyTags(usize),
    DuplicateRule(String),
    MissingRow(String),
    RemovedRule(String),
    NoRules,
}
impl std::fmt::Display for ScopeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{self:?}")
    }
}
impl std::error::Error for ScopeError {}

#[derive(Debug, PartialEq, Eq)]
pub struct RuleRow {
    pub rule_no: String,
    pub roles: Tags,
    pub tags: Tags,
}

#[derive(Debug, Default)]
pub struct RuleScopes {
    pub roles: Tags,
    pub tags: Tags,
    pub projects: BTreeMap<String, Tags>,
    pub kinds: BTreeMap<String, Tags>,
    pub rows: BTreeMap<String, RuleRow>,
}

fn list(text: &str, line: usize) -> Result<Tags, ScopeError> {
    let values: Tags = text.split(',').map(|v| v.trim().to_owned()).collect();
    if values.contains("") {
        return Err(ScopeError::InvalidRow(line));
    }
    Ok(values)
}

fn check_tags(tags: &Tags, vocabulary: &Tags, line: usize) -> Result<(), ScopeError> {
    if tags.len() > 20 {
        return Err(ScopeError::TooManyTags(line));
    }
    if !tags.is_subset(vocabulary) {
        return Err(ScopeError::UnknownTag(line));
    }
    Ok(())
}

impl RuleScopes {
    /// Header directives precede rows; all references are checked against the vocabulary.
    pub fn parse(text: &str) -> Result<Self, ScopeError> {
        let mut scopes = Self::default();
        let rule_no = Regex::new(r"^[0-9]+\.[0-9]+[A-Za-z]?$").unwrap();
        let mut mappings = Vec::new();
        let mut row_lines = BTreeMap::new();
        for (index, raw) in text.lines().enumerate() {
            let line = index + 1;
            if raw.trim().is_empty() {
                continue;
            }
            if let Some(header) = raw.strip_prefix('#') {
                if !scopes.rows.is_empty() {
                    return Err(ScopeError::InvalidHeader(line));
                }
                let fields: Vec<_> = header.trim_start().split('\t').collect();
                match fields.as_slice() {
                    ["roles", values] if scopes.roles.is_empty() => {
                        scopes.roles = list(values, line)?
                    }
                    ["tags", values] if scopes.tags.is_empty() => {
                        scopes.tags = list(values, line)?;
                        if scopes.tags.len() > 20 {
                            return Err(ScopeError::TooManyTags(line));
                        }
                    }
                    [kind @ ("project" | "kind"), name, values] if !name.trim().is_empty() => {
                        mappings.push((line, *kind, *name, list(values, line)?));
                    }
                    _ => return Err(ScopeError::InvalidHeader(line)),
                }
                continue;
            }
            let fields: Vec<_> = raw.split('\t').collect();
            let [number, roles, tags] = fields.as_slice() else {
                return Err(ScopeError::InvalidRow(line));
            };
            if !rule_no.is_match(number) {
                return Err(ScopeError::InvalidRow(line));
            }
            let row = RuleRow {
                rule_no: (*number).into(),
                roles: list(roles, line)?,
                tags: list(tags, line)?,
            };
            if scopes.rows.insert((*number).into(), row).is_some() {
                return Err(ScopeError::DuplicateRule((*number).into()));
            }
            row_lines.insert((*number).to_owned(), line);
        }
        if !scopes.roles.contains("all") || !scopes.tags.contains("all") {
            return Err(ScopeError::InvalidHeader(0));
        }
        for (line, kind, name, tags) in mappings {
            check_tags(&tags, &scopes.tags, line)?;
            let map = if kind == "project" {
                &mut scopes.projects
            } else {
                &mut scopes.kinds
            };
            if map.insert(name.into(), tags).is_some() {
                return Err(ScopeError::InvalidHeader(line));
            }
        }
        for (number, row) in &scopes.rows {
            let line = row_lines[number];
            if !row.roles.is_subset(&scopes.roles) {
                return Err(ScopeError::UnknownRole(line));
            }
            check_tags(&row.tags, &scopes.tags, line)?;
        }
        Ok(scopes)
    }

    /// Fails closed on empty input, missing scope rows and rows for removed rules.
    pub fn validate(&self, decisions: &str) -> Result<(), ScopeError> {
        let rules = decision_rules(decisions);
        if rules.is_empty() {
            return Err(ScopeError::NoRules);
        }
        for number in &rules {
            if !self.rows.contains_key(number) {
                return Err(ScopeError::MissingRow(number.clone()));
            }
        }
        for number in self.rows.keys() {
            if !rules.contains(number) {
                return Err(ScopeError::RemovedRule(number.clone()));
            }
        }
        Ok(())
    }

    /// Unmapped project/kind values add nothing; labels are unioned, never inferred.
    pub fn item_tags(&self, project: &str, kind: &str, scope_labels: &Tags) -> Tags {
        scope_labels
            .iter()
            .chain(self.projects.get(project).into_iter().flatten())
            .chain(self.kinds.get(kind).into_iter().flatten())
            .cloned()
            .collect()
    }
}

/// Preserves letter suffixes and leading zeroes (0.2a, 2.1A, 4.00).
pub fn decision_rules(decisions: &str) -> Tags {
    Regex::new(r"(?m)^([0-9]+\.[0-9]+[A-Za-z]?)[ \t]+")
        .unwrap()
        .captures_iter(decisions)
        .map(|c| c[1].to_owned())
        .collect()
}

/// Empty item tags select every rule of the role; `all` in a row is a wildcard.
pub fn applies(row: &RuleRow, role: &str, item_tags: &Tags) -> bool {
    (row.roles.contains("all") || row.roles.contains(role))
        && (item_tags.is_empty() || row.tags.contains("all") || !row.tags.is_disjoint(item_tags))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture(name: &str) -> String {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join(name);
        let source = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("fixtures");
        std::fs::copy(source.join(name), &path).unwrap();
        std::fs::read_to_string(path).unwrap()
    }

    fn validate_fixture(name: &str) -> Result<(), ScopeError> {
        RuleScopes::parse(&fixture(name))?.validate(&fixture("DECISIONS.md"))
    }

    fn valid() -> RuleScopes {
        RuleScopes::parse(&fixture("valid.tsv")).unwrap()
    }

    fn rejects(text: &str, error: ScopeError) {
        assert_eq!(RuleScopes::parse(text).unwrap_err(), error);
    }

    #[test]
    fn valid_set_passes() {
        assert_eq!(validate_fixture("valid.tsv"), Ok(()));
    }

    #[test]
    fn wrong_tag() {
        assert_eq!(
            validate_fixture("wrong-tag.tsv"),
            Err(ScopeError::UnknownTag(5))
        );
    }

    #[test]
    fn missing_row() {
        assert_eq!(
            validate_fixture("missing-row.tsv"),
            Err(ScopeError::MissingRow("9.14".into()))
        );
    }

    #[test]
    fn extra_row() {
        assert_eq!(
            validate_fixture("extra-row.tsv"),
            Err(ScopeError::RemovedRule("9.99".into()))
        );
    }

    #[test]
    fn empty_tags_item() {
        let scopes = valid();
        let item = fixture("empty-tags-item.tsv");
        let fields: Vec<_> = item.trim_end_matches(['\r', '\n']).split('\t').collect();
        let tags = scopes.item_tags(fields[0], fields[1], &Tags::new());
        assert!(tags.is_empty());
        let role = fields[2];
        assert!(scopes.rows.values().all(|row| applies(row, role, &tags)));
    }

    #[test]
    fn applies_intersection() {
        let scopes = valid();
        let row = &scopes.rows["0.2a"];
        let tags = Tags::from(["ops".into(), "code".into()]);
        assert!(applies(row, "writer", &tags));
        assert!(!applies(row, "writer", &Tags::from(["ops".into()])));
        assert!(!applies(row, "reviewer", &Tags::from(["code".into()])));
        assert!(applies(&scopes.rows["1.1"], "other", &tags));
    }

    #[test]
    fn applies_empty_tags_still_checks_role() {
        let scopes = valid();
        assert!(applies(&scopes.rows["0.2a"], "writer", &Tags::new()));
        assert!(!applies(&scopes.rows["0.2a"], "reviewer", &Tags::new()));
    }

    #[test]
    fn mapped_tags_plus_scope_labels() {
        let scopes = valid();
        let labels = Tags::from(["label".into(), "code".into()]);
        let expected = Tags::from(["code".into(), "ops".into(), "label".into()]);
        assert_eq!(scopes.item_tags("demo", "deploy", &labels), expected);
    }

    #[test]
    fn rejects_bad_vocabulary_and_duplicates() {
        let valid = fixture("valid.tsv");
        let text = valid.replace("0.2a\twriter", "0.2a\tother");
        rejects(&text, ScopeError::UnknownRole(5));
        rejects(
            &format!("{valid}1.1\tall\tall\n"),
            ScopeError::DuplicateRule("1.1".into()),
        );
        let text = valid.replace("# project\tdemo\tcode", "# project\tdemo\twrong");
        rejects(&text, ScopeError::UnknownTag(3));
        let text = valid.replace("all,writer,reviewer", "writer,reviewer");
        rejects(&text, ScopeError::InvalidHeader(0));
        let values: Vec<_> = (0..21).map(|n| format!("t{n}")).collect();
        let many = values.join(",");
        let text = valid.replace("all,code,ops", &many);
        rejects(&text, ScopeError::TooManyTags(2));
        let text = valid.replace("0.2a\twriter\tcode", &format!("0.2a\twriter\t{many}"));
        rejects(&text, ScopeError::TooManyTags(5));
    }

    #[test]
    fn extracts_only_leading_rules_and_rejects_empty_rules() {
        assert_eq!(decision_rules("2.1A **Title.** Text\n4.00 **Title.** Text\n 1.2 indented\nSee 3.4\n1.2.3 nested\n"), Tags::from(["2.1A".into(), "4.00".into()]));
        assert_eq!(valid().validate("# no rules"), Err(ScopeError::NoRules));
    }

    #[test]
    fn rejects_malformed_input() {
        for text in [
            "",
            "# roles\tall\n# tags\tall\n1.1\tall\n",
            "# roles\tall\n# tags\tall\n1.1\t\tall\n",
        ] {
            assert!(RuleScopes::parse(text).is_err());
        }
        let valid = fixture("valid.tsv");
        assert!(RuleScopes::parse(&valid.replace("0.2a", "0.2a.bad")).is_err());
        assert!(RuleScopes::parse(&format!("{valid}# tags\tall\n")).is_err());
        assert!(RuleScopes::parse(&valid.replace("# kind", "# unknown")).is_err());
    }
}
