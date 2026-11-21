//! Part-entry predicate (landing design, replay note P2): a part may not carry foreign history.
//!
//! `pg-policy --part-entry BASE HEAD PARENT|- [OTHER_HEAD...]` prints one `refused <reason>: <detail>`
//! line per violated condition and exits 1; it prints nothing for a part that may enter.
//! OTHER_HEADs are the current heads of the open pull requests that are not in the part's own chain.

fn git_ok(args: &[&str]) -> bool {
    std::process::Command::new("git")
        .args(args)
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

fn is_ancestor(ancestor: &str, descendant: &str) -> bool {
    git_ok(&["merge-base", "--is-ancestor", ancestor, descendant])
}

fn commit(revision: &str) -> Result<String, String> {
    let arg = format!("{revision}^{{commit}}");
    super::git(&["rev-parse", "--verify", &arg]).map(|s| s.trim().to_owned())
}

pub fn run(args: &[String]) -> Result<Vec<String>, String> {
    if args.len() < 3 {
        return Err("--part-entry needs BASE HEAD PARENT|- [OTHER_HEAD...]".into());
    }
    let base = commit(&args[0])?;
    let head = commit(&args[1])?;
    let parent = if args[2] == "-" {
        None
    } else {
        Some(commit(&args[2])?)
    };
    let others = args[3..]
        .iter()
        .map(|o| commit(o))
        .collect::<Result<Vec<_>, _>>()?;
    let mut refused = Vec::new();

    // Own range: the parent's registered head for a child, else the merge base with the landing base.
    let diff_base = match &parent {
        Some(q) => q.clone(),
        None => super::git(&["merge-base", &base, &head])?.trim().to_owned(),
    };
    let merges = super::git(&[
        "rev-list",
        "--merges",
        "--parents",
        &format!("{diff_base}..{head}"),
    ])?;
    for line in merges.lines() {
        let mut ids = line.split_whitespace();
        let merge = ids.next().unwrap_or("");
        for other in ids.skip(1) {
            let known = is_ancestor(other, &base)
                || parent.as_deref().is_some_and(|q| is_ancestor(other, q));
            if !known {
                refused.push(format!(
                    "refused imported_history: merge {} takes {}, which is neither in the landing base nor in the parent head",
                    &merge[..12.min(merge.len())],
                    &other[..12.min(other.len())]
                ));
            }
        }
    }

    for other in &others {
        if *other == head || is_ancestor(other, &base) {
            continue; // an equal head is the duplicate rule's; a landed head is no dependency
        }
        if is_ancestor(other, &head) {
            refused.push(format!(
                "refused undeclared_dependency: the part is built on open head {} without naming it as its base",
                &other[..12.min(other.len())]
            ));
        } else if is_ancestor(&head, other) {
            continue; // the other part builds on this one: judged for that part
        } else {
            let bases = super::git(&["merge-base", "--all", &head, other])?;
            if bases.split_whitespace().any(|b| !is_ancestor(b, &base)) {
                refused.push(format!(
                    "refused shared_history: shares commits outside the landing base with open head {}",
                    &other[..12.min(other.len())]
                ));
            }
        }
    }
    Ok(refused)
}
