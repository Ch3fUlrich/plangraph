//! Approved D-4743 V1 validation capabilities; not live-schema application.
pub(super) const SCHEMA: &str = r#"
node Project {
  slug: String
  name: String?
  path: String?
  summary: String?
  repository: String?
  @key(slug)
}
node Decision {
  slug: String
  title: String
  rationale: String?
  status: enum(proposed, accepted, superseded, deprecated)
  date: String?
  embedding: Vector(768)? @embed("rationale")
  @key(slug)
  @index(status)
}
node Rule {
  slug: String
  statement: String
  severity: enum(must, should)
  @key(slug)
  @index(severity)
}
node Preference {
  slug: String
  statement: String
  scope: String
  @key(slug)
  @index(scope)
}
node Convention {
  slug: String
  name: String
  example: String?
  @key(slug)
}
node Component {
  slug: String
  name: String
  kind: String?
  location: String?
  @key(slug)
}
node Task {
  slug: String
  title: String
  state: String
  @key(slug)
}
edge DecidedIn: Decision -> Project
edge ConstrainsProject: Rule -> Project
edge AppliesTo: Convention -> Project
edge PartOf: Component -> Project
edge Tracks: Task -> Project
edge ConstrainsComponent: Rule -> Component
edge Affects: Decision -> Component
edge Addresses: Task -> Component
edge Implements: Task -> Decision
edge DependsOn: Component -> Component
edge Supersedes: Decision -> Decision
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

#[test]
fn production_schema_capabilities_match_approved_fixture() {
    let production = crate::Capabilities::from_schema(SCHEMA);
    let fixture =
        crate::Capabilities::from_schema(include_str!("../tests/fixtures/memory-v1-approved.pg"));
    assert_eq!(
        (production.nodes, production.edges),
        (fixture.nodes, fixture.edges)
    );
}
