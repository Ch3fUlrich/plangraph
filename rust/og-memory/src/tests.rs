use super::*;
const SCHEMA: &str = include_str!("../tests/fixtures/memory.pg");
fn refused(e: &Envelope, schema: &str, want: Refusal) {
    assert_eq!(
        validate(e, &Capabilities::from_schema(schema)).unwrap_err(),
        want
    );
}
fn fact() -> Envelope {
    Envelope {
        version: 1,
        project: "global".into(),
        kind: "Decision".into(),
        slug: "use-rust".into(),
        content: "Use typed facts".into(),
        source: Source {
            r#type: "file".into(),
            path: "notes.md".into(),
            hash: "h1".into(),
        },
        actor: "builder".into(),
        relations: vec![],
    }
}
#[test]
fn every_slug_rejects_routing_and_bad_grammar() {
    for field in 0..3 {
        for slug in ["routing-owner", "Bad!"] {
            let mut e = fact();
            e.relations.push(Relation {
                edge: "DecidedIn".into(),
                target_slug: "global".into(),
            });
            *[&mut e.project, &mut e.slug, &mut e.relations[0].target_slug][field] = slug.into();
            let want = if slug.starts_with("routing-") {
                Refusal::RoutingSlug
            } else if field == 0 {
                Refusal::InvalidProject
            } else {
                Refusal::InvalidSlug
            };
            refused(&e, SCHEMA, want);
        }
    }
}
#[test]
fn edges_require_live_kind_and_project_endpoints() {
    let mut e = fact();
    e.relations.push(Relation {
        edge: "ConstrainsProject".into(),
        target_slug: "global".into(),
    });
    refused(&e, SCHEMA, Refusal::MissingEdge);
    e.relations[0].edge = "DecidedIn".into();
    assert!(validate(&e, &Capabilities::from_schema(SCHEMA)).is_ok());
    for endpoints in ["Rule -> Project", "Decision -> Decision"] {
        let schema = SCHEMA.replace("Decision -> Project", endpoints);
        refused(&e, &schema, Refusal::MissingEdge);
    }
}
#[test]
fn all_kinds_and_global() {
    for kind in ["Decision", "Rule", "Convention", "Preference"] {
        let mut e = fact();
        e.kind = kind.into();
        let v = validate(&e, &Capabilities::from_schema(SCHEMA)).unwrap();
        assert_eq!(v.envelope().project, "global");
        assert!(!v.properties().is_empty());
        assert_eq!(v.hub_edge().is_empty(), kind == "Preference");
    }
}
#[test]
fn each_refusal() {
    type Case = (Refusal, fn(&mut Envelope));
    let cases: Vec<Case> = vec![
        (Refusal::UnsupportedVersion, |e| e.version = 2),
        (Refusal::UnknownKind, |e| e.kind = "Other".into()),
        (Refusal::Task, |e| e.kind = "Task".into()),
        (Refusal::RoutingSlug, |e| e.slug = "routing-owner".into()),
        (Refusal::InvalidProject, |e| e.project.clear()),
        (Refusal::InvalidProject, |e| e.project = "Bad Hub".into()),
        (Refusal::InvalidSlug, |e| e.slug.clear()),
        (Refusal::InvalidSlug, |e| e.slug = "Bad!".into()),
        (Refusal::MissingSourceHash, |e| e.source.hash.clear()),
        (Refusal::MissingEdge, |e| {
            e.relations.push(Relation {
                edge: "Absent".into(),
                target_slug: "other".into(),
            })
        }),
    ];
    for (want, edit) in cases {
        let mut e = fact();
        edit(&mut e);
        refused(&e, SCHEMA, want);
    }
}
#[test]
fn live_schema_gates_every_mapped_property() {
    for m in V1 {
        for (property, _) in m.2 {
            let mut caps = Capabilities::from_schema(SCHEMA);
            caps.nodes.get_mut(m.0).unwrap().remove(*property);
            let mut e = fact();
            if m.0 != "Project" {
                e.kind = m.0.into();
            }
            assert_eq!(
                validate(&e, &caps).unwrap_err(),
                Refusal::MissingProperty(m.0, property)
            );
        }
    }
    let mut e = fact();
    e.kind = "Preference".into();
    e.relations.push(Relation {
        edge: V1[2].1.into(),
        target_slug: "global".into(),
    });
    refused(&e, SCHEMA, Refusal::MissingEdge);
    let schema = SCHEMA.replace("node Decision {", "node Removed {");
    refused(&fact(), &schema, Refusal::MissingType("Decision"));
    let schema = SCHEMA.replace("edge DecidedIn:", "edge Removed:");
    refused(&fact(), &schema, Refusal::MissingEdge);
}
#[test]
fn secrets_in_every_text_field_are_payload_free() {
    let secrets = [
        format!("{}{}", "sk-", "privatevalue123"),
        format!("Bearer {}", "a1".repeat(12)),
        format!(
            "{}PRIVATE KEY-----\nprivate\n-----END PRIVATE KEY-----",
            "-----BEGIN "
        ),
    ];
    for secret in secrets {
        for field in 0..10 {
            let mut e = fact();
            e.relations.push(Relation {
                edge: "Supersedes".into(),
                target_slug: "old".into(),
            });
            let r = &mut e.relations[0];
            *[
                &mut e.project,
                &mut e.kind,
                &mut e.slug,
                &mut e.content,
                &mut e.source.r#type,
                &mut e.source.path,
                &mut e.source.hash,
                &mut e.actor,
                &mut r.edge,
                &mut r.target_slug,
            ]
            .into_iter()
            .nth(field)
            .unwrap() = secret.clone();
            let err = validate(&e, &Capabilities::from_schema(SCHEMA)).unwrap_err();
            assert_eq!(err, Refusal::Secret);
            assert_eq!(format!("{err}"), "Secret");
            assert_eq!(format!("{err:?}"), "Secret");
        }
    }
}
