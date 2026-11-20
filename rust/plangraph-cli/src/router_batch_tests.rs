use super::*;

const H: &str = "0123456789abcdef0123456789abcdef01234567";
const G: &str = "fedcba9876543210fedcba9876543210fedcba98";

fn part(name: &str, head: &str, writer: &str, reviewer: &str) -> Part {
    Part {
        name: name.into(),
        head: head.into(),
        ci: "green".into(),
        review_file: format!("review-{name}.txt"),
        writer_family: writer.into(),
        reviewer_family: reviewer.into(),
    }
}
fn go(parts: Vec<Part>) -> GoInput {
    GoInput {
        decision: "D-6400".into(),
        pr: 1090,
        head: H.into(),
        parts,
    }
}

#[test]
fn a_batch_pr_with_two_parts_yields_one_go_row_naming_both_heads_reviews_and_families() {
    let row = go_row(&go(vec![
        part("alpha", H, "claude", "gemini"),
        part("beta", G, "claude", "deepseek"),
    ]))
    .unwrap();
    assert_eq!(row.lines().count(), 1);
    assert!(row.starts_with("| D-6400 | GO #1090 GO-HEAD 0123456789abcdef"));
    for needle in [
        format!("alpha head {H} ci green review review-alpha.txt writer claude reviewer gemini"),
        format!("beta head {G} ci green review review-beta.txt writer claude reviewer deepseek"),
        "parts 2".to_string(),
    ] {
        assert!(row.contains(&needle), "{needle} in {row}");
    }
}

#[test]
fn any_bad_part_refuses_the_whole_row_and_names_the_part() {
    let mut red = part("beta", G, "claude", "gemini");
    red.ci = "failing".into();
    assert_eq!(
        go_row(&go(vec![part("alpha", H, "claude", "gemini"), red])).unwrap_err(),
        "part beta: CI is not green"
    );
    assert_eq!(
        go_row(&go(vec![part("alpha", H, "claude", "claude")])).unwrap_err(),
        "part alpha: reviewer family is the writer family"
    );
    assert_eq!(
        go_row(&go(vec![part("alpha", "abc", "claude", "gemini")])).unwrap_err(),
        "part alpha: head is not 40 hex"
    );
    assert!(go_row(&go(vec![])).is_err());
    let mut bad_head = go(vec![part("a", H, "x", "y")]);
    bad_head.head = "short".into();
    assert!(go_row(&bad_head).is_err());
}

#[test]
fn two_grants_land_in_one_row_file() {
    let input = GrantInput {
        decision: "D-6401".into(),
        grants: vec![
            Grant {
                lane: "lane-a".into(),
                grantee: "build-0001".into(),
                scope: "merge".into(),
            },
            Grant {
                lane: "lane-b".into(),
                grantee: "build-0002".into(),
                scope: "merge".into(),
            },
        ],
    };
    let text = grant_rows(&input).unwrap();
    assert_eq!(
        text,
        "| D-6401 | GRANT lane-a to build-0001 scope merge |\n| D-6401 | GRANT lane-b to build-0002 scope merge |\n"
    );
    let mut twice = input;
    twice.grants[1].lane = "lane-a".into();
    assert!(grant_rows(&twice).unwrap_err().contains("listed twice"));
}

#[test]
fn related_asks_go_out_as_one_numbered_group() {
    let input = AskInput {
        to: "plan-order-0001".into(),
        topic: "batch 11 scope".into(),
        asks: vec![
            Ask {
                item: "p:A".into(),
                question: "split?".into(),
            },
            Ask {
                item: "p:B".into(),
                question: "defer?".into(),
            },
        ],
    };
    assert_eq!(
        ask_group(&input).unwrap(),
        "ASK-GROUP plan-order-0001 | batch 11 scope | 2 item(s)\n1. p:A | split?\n2. p:B | defer?\n"
    );
    let mut multi = input;
    multi.asks[0].question = "two\nlines".into();
    assert!(ask_group(&multi).is_err());
}

#[test]
fn the_router_skill_holds_the_batch_section_for_every_successor() {
    let skill = std::fs::read_to_string(
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../skills/router-core/SKILL.md"),
    )
    .unwrap();
    assert!(skill.starts_with("---\nname: router-core\n"));
    for needle in [
        "## Batch mode",
        "tools/apply_batch.py",
        "plangraph router go-row",
        "plangraph router grant-rows",
        "plangraph router ask-group",
        "nothing applied",
    ] {
        assert!(skill.contains(needle), "skill lacks {needle}");
    }
}
