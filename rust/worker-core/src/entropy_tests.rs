use super::*;

#[test]
fn changelog_fragments_are_not_new_plan_documents() {
    assert!(!plan_report("changelog.d/PLAN-LINT.md"));
    assert!(plan_report("docs/plans/new.md"));
    assert!(plan_report("reports/measurement.md"));
}
fn fixture() -> Input {
    Input {
        changes: vec![Change {
            path: "src/tool.rs".into(),
            added: 10,
            deleted: 2,
            new: false,
        }],
        current: Inventory {
            classes: [("source".into(), 1000)].into(),
            source: 1000,
            tests: 100,
            largest_test: 100,
        },
        week: Inventory {
            classes: [("source".into(), 1000)].into(),
            source: 1000,
            tests: 100,
            largest_test: 100,
        },
        baseline: Baseline::default(),
        prior_baseline: Some(Baseline::default()),
        body: String::new(),
    }
}
#[test]
fn entropy_five_fixture_prs() {
    let pass = fixture();
    let mut large = fixture();
    large.changes[0].added = 401;
    let mut heavy = fixture();
    heavy.changes.push(Change {
        path: "tests/test_tool.rs".into(),
        added: 11,
        deleted: 0,
        new: false,
    });
    let mut markdown = fixture();
    markdown.changes.push(Change {
        path: "docs/plan/new.md".into(),
        added: 2,
        deleted: 0,
        new: true,
    });
    let mut long_test = fixture();
    long_test.current.largest_test = 401;
    let decisions: Vec<_> = [pass, large, heavy, markdown, long_test]
        .iter()
        .map(|p| check(p).is_empty())
        .collect();
    assert_eq!(decisions, [true, false, false, false, false]);
}
#[test]
fn entropy_waivers_are_scoped_and_baselines_only_shrink() {
    let mut p = fixture();
    p.changes[0].added = 401;
    assert_eq!(check(&p)[0].code, "G1");
    p.body = "Plan-Slice: bounded collector\n".into();
    assert!(check(&p).is_empty());
    p.changes.push(Change {
        path: "tests/new.rs".into(),
        added: 402,
        deleted: 0,
        new: true,
    });
    assert!(check(&p).iter().any(|f| f.code == "G3-added"));
    p.body.push_str("Test-Heavy: regression matrix\n");
    assert!(check(&p).is_empty());
    p.baseline.test_source_ratio = 0.21;
    assert!(check(&p).iter().any(|f| f.code == "baseline"));
    p.baseline.test_source_ratio = 0.09;
    assert!(check(&p).iter().any(|f| f.code == "G3-ratio"));
}
#[test]
fn entropy_weekly_class_growth_and_all_failures() {
    let mut p = fixture();
    p.current.classes.insert("source".into(), 1031);
    p.current.largest_test = 401;
    p.changes[0].added = 401;
    let codes: Vec<_> = check(&p).into_iter().map(|f| f.code).collect();
    assert_eq!(codes, ["G1", "G2", "G3-file"]);
    p.current.classes.insert("source".into(), 1030);
    assert!(!check(&p).iter().any(|f| f.code == "G2"));
}
