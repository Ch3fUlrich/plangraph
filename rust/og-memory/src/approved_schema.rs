//! Approved D-4743 V1 validation capabilities; not live-schema application.
pub(super) const SCHEMA: &str = r#"
node Project {
  slug: String
  @key(slug)
}
node Decision {
  slug: String
  title: String
  rationale: String?
  status: enum(proposed, accepted, superseded, deprecated)
  date: String?
  @key(slug)
}
node Rule {
  slug: String
  statement: String
  severity: enum(must, should)
  @key(slug)
}
node Convention {
  slug: String
  name: String
  example: String?
  @key(slug)
}
node Preference {
  slug: String
  statement: String
  scope: String
  @key(slug)
}
edge DecidedIn: Decision -> Project
edge ConstrainsProject: Rule -> Project
edge AppliesTo: Convention -> Project
node MemoryRecord {
  project: String
  kind: String
  slug: String
  content: String
  version: I32?
  sourceType: String?
  sourcePath: String?
  sourceHash: String?
  actor: String?
  relationsJson: String?
  @key(project, kind, slug)
}
node MemorySavedState {
  project: String
  kind: String
  slug: String
  receiptJson: String
  superseded: Bool
  @key(project, kind, slug)
}
"#;
