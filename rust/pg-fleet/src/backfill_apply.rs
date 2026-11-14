use crate::executor::{strings, Executor};
use std::path::{Path, PathBuf};
pub struct Options {
    pub apply: bool,
    pub go: Option<String>,
    pub rows: Option<PathBuf>,
    pub dry_run: bool,
    pub runs: Option<PathBuf>,
    pub writer_family: Option<String>,
    pub token_dir: Option<PathBuf>,
    pub url: Option<String>,
    pub repo: PathBuf,
    pub pgk: PathBuf,
    pub python: String,
}
pub fn shell_join(args: &[String]) -> String {
    args.iter()
        .map(|s| {
            if !s.is_empty()
                && s.chars()
                    .all(|c| c.is_ascii_alphanumeric() || "_@%+=:,./-".contains(c))
            {
                s.clone()
            } else {
                format!("'{}'", s.replace('\'', "'\"'\"'"))
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}
fn runs(dir: &Path, run: &str) -> Vec<PathBuf> {
    let mut found = vec![];
    let Ok(root) = dir.canonicalize() else {
        return found;
    };
    let mut stack = vec![dir.to_owned()];
    while let Some(d) = stack.pop() {
        let Ok(entries) = std::fs::read_dir(d) else {
            continue;
        };
        for e in entries.flatten() {
            let p = e.path();
            let Ok(kind) = e.file_type() else { continue };
            if !kind.is_dir() {
                continue;
            }
            let name = e.file_name();
            let name = name.to_string_lossy();
            if name
                .strip_prefix(run)
                .is_some_and(|rest| rest.chars().all(|c| c.is_ascii_hexdigit()))
                && p.canonicalize().is_ok_and(|p| p.starts_with(&root))
            {
                found.push(p.clone());
            }
            stack.push(p);
        }
    }
    found
}
fn command(
    r: &mut dyn Executor,
    argv: &[String],
    label: Option<&str>,
    dry_run: bool,
    out: &mut String,
) -> i32 {
    if dry_run {
        out.push_str(&shell_join(argv));
        out.push('\n');
        return 0;
    }
    let result = r.run(&argv[0], &argv[1..], 600, true);
    let (code, output) = match result {
        Ok(o) => (o.code, o.stdout),
        Err(e) => (1, format!("subprocess failed: {e}")),
    };
    let last = output.lines().last().unwrap_or("");
    if let Some(label) = label {
        out.push_str(&format!("{label} rc={code} {last}\n"));
    } else {
        out.push_str(if code == 0 {
            last
        } else {
            output.trim_end_matches('\n')
        });
        out.push('\n');
    }
    code
}
pub fn apply(o: &Options, report: &[Vec<String>], r: &mut dyn Executor) -> (i32, String) {
    if !(o.apply && o.go.is_some() && o.rows.is_some()) {
        return (0, String::new());
    }
    let selected = std::fs::read_to_string(o.rows.as_ref().unwrap()).unwrap_or_default();
    let mut seen = std::collections::HashSet::new();
    let mut out = String::new();
    let mut counts = [0, 0, 0];
    let mut code = 0;
    let record = |rc: i32, refused: bool, counts: &mut [usize; 3], code: &mut i32| {
        if !o.dry_run {
            counts[if refused {
                1
            } else if rc != 0 {
                2
            } else {
                0
            }] += 1;
            if *code == 0 {
                *code = rc;
            }
        } else if refused {
            counts[1] += 1;
        }
    };
    for key in selected.lines().map(str::trim).filter(|s| !s.is_empty()) {
        if !seen.insert(key) {
            continue;
        }
        let row = report.iter().find(|row| row[0] == key);
        if row.is_none() || row.unwrap()[6] == "no-go-row" {
            out.push_str(&format!(
                "{key} refused: {}\n",
                if row.is_none() {
                    "not in current report"
                } else {
                    "no-go-row"
                }
            ));
            record(2, true, &mut counts, &mut code);
            continue;
        }
        let row = row.unwrap();
        if row[6] == "post-verdicts" {
            for run in row[5].split(',') {
                let matches = o.runs.as_deref().map_or_else(Vec::new, |p| runs(p, run));
                if matches.len() != 1
                    || o.writer_family.as_ref().is_none_or(|s| s.is_empty())
                    || o.token_dir.is_none()
                {
                    out.push_str(&format!("{key} {run} rc=2 refused: need unique run directory, --writer-family and --token-dir\n"));
                    record(2, true, &mut counts, &mut code);
                    continue;
                }
                let seat = &matches[0];
                let mut argv = vec![
                    o.python.clone(),
                    o.repo
                        .join("tools/seat_verdict.py")
                        .to_string_lossy()
                        .into_owned(),
                    "--key".into(),
                    key.into(),
                    "--seat-dir".into(),
                    seat.to_string_lossy().into_owned(),
                    "--writer-family".into(),
                    o.writer_family.clone().unwrap(),
                    "--token-dir".into(),
                    o.token_dir.as_ref().unwrap().to_string_lossy().into_owned(),
                ];
                if !seat.join("evidence.json").exists() && seat.join("record.json").exists() {
                    argv.extend(["--head".into(), row[2].clone()]);
                }
                if let Some(url) = &o.url {
                    argv.extend(["--url".into(), url.clone()]);
                }
                let rc = command(r, &argv, Some(&format!("{key} {run}")), o.dry_run, &mut out);
                record(rc, [2, 4].contains(&rc), &mut counts, &mut code);
            }
        } else {
            let argv = strings(&[
                "bash",
                &o.pgk.to_string_lossy(),
                "evidence",
                key,
                "--kind",
                "decision",
                "--decision",
                o.go.as_ref().unwrap(),
            ]);
            let mut rc = command(r, &argv, None, o.dry_run, &mut out);
            if rc == 0 {
                rc = command(
                    r,
                    &strings(&["bash", &o.pgk.to_string_lossy(), "submit", key]),
                    None,
                    o.dry_run,
                    &mut out,
                );
            }
            record(rc, rc == 2, &mut counts, &mut code);
        }
    }
    out.push_str(&format!(
        "sent={} refused={} failed={}\n",
        counts[0], counts[1], counts[2]
    ));
    (code, out)
}
