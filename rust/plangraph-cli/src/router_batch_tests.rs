use super::*;

const H: &str = "0123456789abcdef0123456789abcdef01234567";
const G: &str = "fedcba9876543210fedcba9876543210fedcba98";

fn seat(name: &str, n: u8, reviewer: &str) -> Seat {
    Seat {
        review_file: format!("review-{name}-{n}.txt"),
        reviewer_family: reviewer.into(),
    }
}
/// A part with its two required seats: `reviewer` and a fixed second family.
fn part(name: &str, head: &str, writer: &str, reviewer: &str) -> Part {
    Part {
        name: name.into(),
        head: head.into(),
        ci: "green".into(),
        writer_family: writer.into(),
        seats: vec![seat(name, 1, reviewer), seat(name, 2, "glm")],
    }
}
/// Review files by name: every seat's file accepts the head the part names, with FILES READ.
fn accepting(path: &str) -> Result<String, String> {
    let name = path
        .trim_start_matches("review-")
        .split('-')
        .next()
        .unwrap();
    let head = if name == "beta" { G } else { H };
    Ok(format!(
        "FILES READ: src/lib.rs\nHEAD: {head}\nHARNESS: opencode\nVERDICT: ACCEPT\n"
    ))
}
fn go(parts: Vec<Part>) -> GoInput {
    GoInput {
        decision: "D-6400".into(),
        project: "demo".into(),
        pr: 1090,
        head: H.into(),
        parts,
    }
}

fn go_row_ok(input: &GoInput) -> Result<String, String> {
    go_row(input, &accepting)
}

#[test]
fn a_batch_pr_with_two_parts_yields_one_go_row_naming_both_heads_reviews_and_families() {
    let row = go_row_ok(&go(vec![
        part("alpha", H, "claude", "gemini"),
        part("beta", G, "claude", "deepseek"),
    ]))
    .unwrap();
    assert_eq!(row.lines().count(), 1);
    assert!(row.starts_with("| D-6400 | demo | GO #1090 GO-HEAD 0123456789abcdef"));
    for needle in [
        format!("alpha head {H} ci green writer claude review review-alpha-1.txt reviewer gemini review review-alpha-2.txt reviewer glm"),
        format!("beta head {G} ci green writer claude review review-beta-1.txt reviewer deepseek review review-beta-2.txt reviewer glm"),
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
        go_row_ok(&go(vec![part("alpha", H, "claude", "gemini"), red])).unwrap_err(),
        "part beta: CI is not green"
    );
    assert_eq!(
        go_row_ok(&go(vec![part("alpha", H, "claude", "claude")])).unwrap_err(),
        "part alpha: reviewer family is the writer family"
    );
    assert_eq!(
        go_row_ok(&go(vec![part("alpha", "abc", "claude", "gemini")])).unwrap_err(),
        "part alpha: head is not 40 hex"
    );
    assert_eq!(
        go_row_ok(&go(vec![part("alpha", H, "claude", "glm")])).unwrap_err(),
        "part alpha: review seats share a family"
    );
    assert!(go_row_ok(&go(vec![part("alpha", H, "claude", "gemini")])).is_ok());
    assert!(go_row_ok(&go(vec![])).is_err());
    let mut bad_head = go(vec![part("a", H, "x", "y")]);
    bad_head.head = "short".into();
    assert!(go_row_ok(&bad_head).is_err());
}

#[test]
fn two_grants_land_in_one_row_file() {
    let input = GrantInput {
        decision: "D-6401".into(),
        project: "demo".into(),
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
        "| D-6401 | demo | GRANT lane-a to build-0001 scope merge | router | |\n| D-6401 | demo | GRANT lane-b to build-0002 scope merge | router | |\n"
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

#[test]
fn a_review_file_that_is_missing_for_another_head_or_not_an_accept_refuses_the_part() {
    let one = go(vec![part("alpha", H, "claude", "gemini")]);
    let missing = go_row(&one, &|p| Err(format!("{p}: No such file or directory")));
    assert!(missing
        .unwrap_err()
        .starts_with("part alpha: review file unreadable"));
    let other = go_row(&one, &|_| Ok(format!("HEAD: {G}\nVERDICT: ACCEPT\n")));
    assert_eq!(
        other.unwrap_err(),
        "part alpha: review file is not for this head"
    );
    for text in [
        format!("HEAD: {H}\nVERDICT: REQUEST_CHANGES\n"),
        format!("HEAD: {H}\nVERDICT: ACCEPT\nVERDICT: REQUEST_CHANGES\n"),
        format!("HEAD: {H}\n"),
    ] {
        let refused = go_row(&one, &|_| Ok(text.clone()));
        assert_eq!(
            refused.unwrap_err(),
            "part alpha: review file is not an ACCEPT",
            "{text}"
        );
    }
    // A review that names two heads is refused whichever comes first.
    for (first, second) in [(H, G), (G, H)] {
        let mixed = go_row(&one, &|_| {
            Ok(format!(
                "HEAD: {first}\nVERDICT: ACCEPT\nHEAD: {second}\nVERDICT: ACCEPT\n"
            ))
        });
        assert_eq!(
            mixed.unwrap_err(),
            "part alpha: review file is not for this head"
        );
    }
    let no_head = go_row(&one, &|_| Ok("VERDICT: ACCEPT\n".into()));
    assert!(no_head.unwrap_err().contains("not for this head"));
    // The same path through the real file reader: a missing path is refused.
    let nowhere = part("alpha", H, "claude", "gemini");
    let mut input = go(vec![nowhere]);
    input.parts[0].seats[1].review_file = "/nonexistent/review-alpha.txt".into();
    assert!(go_row(&input, &read_review)
        .unwrap_err()
        .contains("review file unreadable"));
}

#[test]
fn a_part_needs_both_required_seats_to_accept_with_files_read() {
    // One seat, even a good one, is not a part (rule 3.66 (f)).
    let mut single = part("alpha", H, "claude", "gemini");
    single.seats.truncate(1);
    assert_eq!(
        go_row_ok(&go(vec![single])).unwrap_err(),
        "part alpha: needs two required review seats"
    );
    let two = go(vec![part("alpha", H, "claude", "gemini")]);
    // An ACCEPT without a FILES READ line, or one that read only the diff, is no seat.
    for text in [
        format!("HEAD: {H}\nVERDICT: ACCEPT\n"),
        format!("FILES READ: diff only\nHEAD: {H}\nVERDICT: ACCEPT\n"),
        format!("FILES READ: none\nHEAD: {H}\nVERDICT: ACCEPT\n"),
        format!("> FILES READ: src/lib.rs\nHEAD: {H}\nVERDICT: ACCEPT\n"),
    ] {
        let refused = go_row(&two, &|_| Ok(text.clone()));
        assert_eq!(
            refused.unwrap_err(),
            "part alpha: review file has no FILES READ",
            "{text}"
        );
    }
    // The second seat alone failing refuses the part: both must ACCEPT.
    let second_declines = go_row(&two, &|path| {
        if path.ends_with("-2.txt") {
            Ok(format!(
                "FILES READ: src/lib.rs\nHEAD: {H}\nVERDICT: REQUEST_CHANGES\n"
            ))
        } else {
            accepting(path)
        }
    });
    assert_eq!(
        second_declines.unwrap_err(),
        "part alpha: review file is not an ACCEPT"
    );
    let second_diff_only = go_row(&two, &|path| {
        if path.ends_with("-2.txt") {
            Ok(format!(
                "FILES READ: diff only\nHEAD: {H}\nVERDICT: ACCEPT\n"
            ))
        } else {
            accepting(path)
        }
    });
    assert_eq!(
        second_diff_only.unwrap_err(),
        "part alpha: review file has no FILES READ"
    );
}

/// The keeper's own reader takes the generated row as one GO bound to the PR and the head.
#[test]
fn the_generated_go_row_is_read_by_the_keepers_parser_as_a_go_bound_to_pr_and_head() {
    let row = go_row_ok(&go(vec![
        part("alpha", H, "claude", "gemini"),
        part("beta", G, "claude", "deepseek"),
    ]))
    .unwrap();
    let scratch = tempfile::tempdir().unwrap();
    let log = scratch.path().join("DECISIONS.md");
    std::fs::write(
        &log,
        format!("| ID | project | decision | by | why |\n|---|---|---|---|---|\n{row}\n"),
    )
    .unwrap();
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let script = "import sys; sys.path.insert(0, sys.argv[2]); from pathlib import Path\n\
        from plangraph.keeper.evidence_decisions import DecisionsAdapter\n\
        from plangraph.keeper.store_waiver import _go_binding, _go_statement\n\
        text = DecisionsAdapter(Path(sys.argv[1])).text('D-6400')\n\
        print(*_go_binding(_go_statement(text), 'demo'))";
    let python = std::env::var("PG_TEST_PYTHON").unwrap_or_else(|_| "python3".into());
    let out = std::process::Command::new(python)
        .args(["-I", "-c", script])
        .arg(&log)
        .arg(root.join("src"))
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert_eq!(
        String::from_utf8_lossy(&out.stdout).trim(),
        format!("1090 {H}")
    );
}
