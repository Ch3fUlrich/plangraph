use crate::handoff::re;
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct GoRow {
    pub did: String,
    pub decision: String,
    pub heads: Vec<String>,
    pub runs: Vec<String>,
}
pub fn parse_row(row: &str, allow_prefix: bool) -> Option<GoRow> {
    let cells: Vec<_> = row.split('|').collect();
    if cells.len() < 4 || !re(r"^\| D-\d+ \|").is_match(row) {
        return None;
    }
    let width = if allow_prefix { "{8,40}" } else { "{40}" };
    let pattern = format!(r"\bGO-HEAD ([0-9a-fA-F]{width})\b");
    let heads = re(&pattern)
        .captures_iter(row)
        .map(|c| c[1].into())
        .collect();
    let mut runs = vec![];
    for c in re(r"\brun-[0-9a-fA-F]+\b").find_iter(row) {
        if !runs.iter().any(|r| r == c.as_str()) {
            runs.push(c.as_str().into());
        }
    }
    Some(GoRow {
        did: cells[1].trim().into(),
        decision: cells[3].into(),
        heads,
        runs,
    })
}
fn word(c: char) -> bool {
    c.is_alphanumeric() || c == '_' || re(r"^\w$").is_match(&c.to_string())
}
fn boundary(s: &str, idx: usize) -> bool {
    s[..idx]
        .chars()
        .next_back()
        .is_none_or(|c| !word(c) && c != '-')
}
pub fn stop(reason: &str) -> (i32, String) {
    (5, format!("STOP: {reason}\n"))
}

pub fn evaluate(
    text: &str,
    go: &str,
    head: &str,
    key: Option<&str>,
    session: Option<&str>,
    mut keeper: impl FnMut(&str) -> Result<i32, String>,
) -> (i32, String) {
    if !re(r"^[0-9a-fA-F]{40}$").is_match(head) {
        return (2, String::new());
    }
    let Some(row) = text.lines().find(|l| l.starts_with(&format!("| {go} |"))) else {
        return stop("no row");
    };
    if row.split('|').count() < 4 {
        return stop("row too short");
    }
    let parsed = parse_row(row, false);
    if !parsed.is_some_and(|p| p.heads.iter().any(|sha| sha.eq_ignore_ascii_case(head))) {
        return stop("row has no GO-HEAD for head");
    }
    let mut unwrapped = row.as_bytes().to_vec();
    let mut masked = unwrapped.clone();
    for q in re(r#"`[^`|]*`|"[^"|]*"|'[^'|]*'"#).find_iter(row) {
        if row[..q.start()].chars().next_back().is_some_and(word) {
            continue;
        }
        if re(r"(?i)^\s*PLAN\b").is_match(&q.as_str()[1..q.len() - 1]) {
            for i in [q.start(), q.end() - 1] {
                unwrapped[i] = b' ';
                masked[i] = b' ';
            }
        } else {
            masked[q.range()].fill(b' ');
        }
    }
    let declaration = String::from_utf8(unwrapped).unwrap();
    let structure = String::from_utf8(masked).unwrap();
    let mut plans = vec![];
    for c in re(r"(?i)PLAN\b").find_iter(&structure) {
        if !boundary(&structure, c.start()) {
            continue;
        }
        let tail = &structure[c.end()..];
        let complete = re(r"(?i)^\s+[^\s|;]+\s+conforms\s*=").is_match(tail);
        let incomplete = re(r"(?:^|[|;])\s*$").is_match(&structure[..c.start()])
            && re(r"^(?:\s+[^\s|;]+:[^\s|;]+)?\s*(?:[|;]|$)").is_match(tail);
        if complete || incomplete {
            plans.push(c.start());
        }
    }
    if plans.len() > 1 {
        return stop("row has multiple plan blocks");
    }
    if re(r"(?i)conforms\s*=")
        .find_iter(&structure)
        .filter(|m| boundary(&structure, m.start()))
        .count()
        > 1
    {
        return stop("row has multiple conformance fields");
    }
    let plan = plans.first().and_then(|start| {
        re(r"^PLAN\s+([^\s|;]+)\s+conforms=(yes|no)\s+deviations=")
            .captures(&declaration[*start..])
            .map(|c| {
                (
                    *start + c.get(0).unwrap().end(),
                    c[1].to_owned(),
                    c[2].to_owned(),
                )
            })
    });
    let Some((end, plan_key, conforms)) = plan else {
        return stop("row has no plan block");
    };
    let len = re(r"(?:;\s*|\s+)\w[\w-]*=|\||$")
        .find(&structure[end..])
        .map_or(structure.len() - end, |m| m.start());
    let deviations = declaration[end..end + len].trim();
    let Some(key) = key else {
        return stop("--key required for plan block");
    };
    if plan_key != key {
        return stop(&format!("plan key {plan_key} != item {key}"));
    }
    let meaningful = deviations.chars().any(char::is_alphanumeric);
    if conforms == "no" && (!meaningful || re(r"^none(?:$|[\W_])").is_match(deviations)) {
        return stop("conforms=no requires deviations");
    }
    if !meaningful {
        return stop("empty deviations token");
    }
    if conforms == "yes" && deviations != "none" {
        return stop("conforms=yes requires deviations=none");
    }
    match keeper(key) {
        Err(e) => return stop(&e),
        Ok(0) => {}
        Ok(_) => return stop(&format!("no keeper item {key}")),
    }
    if let Some(c) = re(r"\bmerge only by\s+([^\s,;|]+)").captures(row) {
        if session != Some(&c[1]) {
            return stop(&format!("merge only by {}", &c[1]));
        }
    }
    (
        0,
        format!(
            "GO-ROW-OK {go} {} plan={key} conforms={conforms} deviations={deviations}\n",
            &head[..8]
        ),
    )
}

pub fn main(args: &[String]) -> i32 {
    let opts = match crate::cli::options(
        args,
        &["--go", "--head", "--session", "--key", "--log", "--repo"],
        &["--help", "-h"],
    ) {
        Ok(o) => o,
        Err(e) => return crate::cli::error("go-row-gate", &e),
    };
    if opts.contains_key("--help") || opts.contains_key("-h") {
        print!("{}", crate::cli::help("go-row-gate"));
        return 0;
    }
    let (Some(go), Some(head)) = (opts.get("--go"), opts.get("--head")) else {
        let missing = ["--go", "--head"]
            .into_iter()
            .filter(|k| !opts.contains_key(*k))
            .collect::<Vec<_>>()
            .join(", ");
        return crate::cli::error(
            "go-row-gate",
            &format!("the following arguments are required: {missing}"),
        );
    };
    if !re(r"^[0-9a-fA-F]{40}$").is_match(head) {
        return crate::cli::error("go-row-gate", "--head must be exactly 40 hex characters");
    }
    let text = if let Some(log) = opts.get("--log") {
        match std::fs::read_to_string(log) {
            Ok(t) => t,
            Err(_) => {
                print!("{}", stop("no row").1);
                return 5;
            }
        }
    } else {
        let repo = opts
            .get("--repo")
            .map(|s| s.to_string())
            .unwrap_or_else(|| {
                crate::data_home()
                    .parent()
                    .unwrap_or(&crate::data_home())
                    .to_string_lossy()
                    .into_owned()
            });
        let fetched =
            crate::process::run("git", &["-C", &repo, "fetch", "-q", "origin", "main"], 120);
        if !fetched.is_ok_and(|o| o.code == 0) {
            print!("{}", stop("fetch of origin/main failed").1);
            return 5;
        }
        match crate::process::run(
            "git",
            &[
                "-C",
                &repo,
                "show",
                "origin/main:router/docs/decisions-log.md",
            ],
            30,
        ) {
            Ok(o) if o.code == 0 => o.stdout,
            _ => {
                print!("{}", stop("no log on origin/main").1);
                return 5;
            }
        }
    };
    let (code, out) = evaluate(
        &text,
        go,
        head,
        opts.get("--key").map(String::as_str),
        opts.get("--session").map(String::as_str),
        |key| {
            let pgk = crate::merge_paths::lookup("pgk")
                .map_err(|_| "keeper wrapper unavailable".to_owned())?;
            crate::process::run("bash", &[&crate::paths::display(&pgk), "show", key], 10)
                .map(|o| o.code)
                .map_err(|e| {
                    if e == "TimeoutExpired" {
                        format!("keeper timed out {key}")
                    } else {
                        "keeper wrapper unavailable".into()
                    }
                })
        },
    );
    print!("{out}");
    code
}
