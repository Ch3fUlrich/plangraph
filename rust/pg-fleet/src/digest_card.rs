use crate::digest::State;
use crate::digest_text::*;
use crate::fleet_name::truth;
use crate::handoff::{re, Card};
use std::path::{Path, PathBuf};

pub fn abs(p: &str, base: &Path) -> PathBuf {
    let p = crate::paths::expand(Path::new(p));
    if p.is_absolute() {
        p
    } else {
        base.join(p)
    }
}
pub fn sibling(p: &Path, to_card: bool) -> PathBuf {
    let name = p.file_name().unwrap_or_default().to_string_lossy();
    if to_card {
        if name.ends_with(".card.md") {
            p.into()
        } else {
            p.with_file_name(format!(
                "{}.card.md",
                p.file_stem().unwrap_or_default().to_string_lossy()
            ))
        }
    } else {
        p.with_file_name(
            name.strip_suffix(".card.md")
                .map_or_else(|| name.to_string(), |s| format!("{s}.md")),
        )
    }
}
fn newest(paths: &[PathBuf]) -> Option<PathBuf> {
    paths
        .iter()
        .filter(|p| usable(p))
        .max_by_key(|p| mtime(p))
        .cloned()
}
fn belongs(role_: &str, p: &Path) -> bool {
    re(&format!(r"^{}(?:-\d+)?\.card\.md$", regex::escape(role_)))
        .is_match(&p.file_name().unwrap_or_default().to_string_lossy())
        || crate::handoff::read_card(p)
            .ok()
            .and_then(|t| Card::parse(&t).header)
            .is_some_and(|h| role(&h.1) == role_)
}
pub fn section(s: &mut State) -> Vec<String> {
    if s.role.is_empty() {
        return vec![
            "== Card".into(),
            "note: no role, no card (pass --role)".into(),
        ];
    }
    let first = std::fs::read_to_string(
        s.config
            .routing
            .join("launch")
            .join(format!("{}.restart.md", s.role)),
    )
    .unwrap_or_default();
    let restart = re(r"^Restart from handoff (\S+?)\.(?:\s|$)")
        .captures(first.lines().next().unwrap_or(""))
        .map(|c| c[1].to_owned());
    let value = &s.spec["handoff"];
    let hands: Vec<_> = [
        truth(value).then(|| crate::digest_sources::py(value)),
        restart,
    ]
    .into_iter()
    .flatten()
    .filter(|s| !s.is_empty())
    .map(|p| abs(&p, &s.role_cwd))
    .collect();
    let mut own = vec![];
    let p = if truth(&s.spec["card"]) {
        crate::digest_sources::py(&s.spec["card"])
    } else {
        String::new()
    };
    if !p.is_empty() {
        own.push(abs(&p, &s.role_cwd));
    }
    own.extend(hands.iter().map(|h| sibling(h, true)));
    let card = newest(&own).or_else(|| {
        let pool: Vec<_> = [&s.role_cwd, &s.config.data]
            .into_iter()
            .flat_map(|d| glob(&d.join("status"), ".card.md"))
            .filter(|p| usable(p) && belongs(&s.role, p))
            .collect();
        newest(&pool)
    });
    let mut cands: Vec<_> = hands.iter().map(|h| sibling(h, false)).collect();
    if let Some(p) = &card {
        cands.push(sibling(p, false));
    }
    if s.role == "routing" {
        cands.push(s.role_cwd.join("HANDOFF.md"));
    }
    let hand = newest(&cands);
    s.handoff = hand.clone().or_else(|| cands.first().cloned());
    let file_age = |p: &Path| age((s.config.now - mtime(p).unwrap_or(s.config.now)).num_seconds());
    let hline = if let Some(p) = hand {
        format!("   full handoff (age {}): {}", file_age(&p), p.display())
    } else if let Some(p) = &s.handoff {
        format!("   full handoff (missing): {}", p.display())
    } else {
        "   full handoff: none known".into()
    };
    let Some(card) = card else {
        return vec!["== Card".into(),format!("note: no card for {} (launch record, <role>.restart.md, status/*.card.md in {} and {})",s.role,s.role_cwd.display(),s.config.data.display()),hline];
    };
    let text = match crate::handoff::read_card(&card) {
        Ok(t) => t,
        Err(e) => return vec!["== Card".into(), format!("note: {}", cut(&e, 160)), hline],
    };
    let doc = Card::parse(&text);
    s.card_ts = doc
        .stamp()
        .map(|t| t.and_utc())
        .into_iter()
        .chain(mtime(&card))
        .min();
    s.drill = doc
        .rows("Drill-down")
        .iter()
        .map(|(_, s)| s.clone())
        .collect();
    s.card_text = text.clone();
    let clean = re(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]")
        .replace_all(&text, "");
    let mut lines: Vec<_> = clean
        .split(['\n', '\r', '\u{2028}', '\u{2029}'])
        .map(|ln| plain(ln).trim_end().chars().take(400).collect::<String>())
        .collect();
    while lines.last().is_some_and(|s| s.trim().is_empty()) {
        lines.pop();
    }
    let count = lines.len();
    let mut out = vec![
        format!("== Card (age {}): {}", file_age(&card), card.display()),
        hline,
    ];
    out.extend(lines.into_iter().take(40));
    if count > 40 {
        out.push(format!(
            "... +{} more card lines (see the file)",
            count - 40
        ));
    }
    out
}
