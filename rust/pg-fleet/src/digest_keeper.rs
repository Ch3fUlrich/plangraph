use crate::digest::State;
use crate::digest_text::*;
use crate::fleet_name::{string, truth};
use crate::handoff::re;
use serde_json::Value;

pub fn prio(v: &Value) -> i64 {
    v["priority"].as_i64().unwrap_or(0)
}
pub fn ready_items(items: &[Value], aliases: &Value, project: &str) -> (Vec<Value>, usize) {
    let open: Vec<_> = items
        .iter()
        .filter(|i| string(i, "project") == project && i["state"] == "open")
        .collect();
    let ready = open
        .iter()
        .filter(|i| {
            ["lane", "gate"].contains(&string(i, "kind"))
                && !truth(&i["operator"])
                && !truth(&i["waits_on_operator"])
                && i["depends_on"].as_array().into_iter().flatten().all(|k| {
                    k.as_str().is_some_and(|k| {
                        items
                            .iter()
                            .find(|i| string(i, "key") == k)
                            .or_else(|| {
                                aliases[k]
                                    .as_str()
                                    .and_then(|a| items.iter().find(|i| string(i, "key") == a))
                            })
                            .is_some_and(|d| d["state"] == "done")
                    })
                })
        })
        .map(|i| (*i).clone())
        .collect();
    (ready, open.len())
}

fn holder(s: &State, i: &Value) -> bool {
    // Once whoami is known the actual claim is authoritative. An owner role
    // cannot make another principal's active claim ours.
    if let Some(principal) = &s.config.principal {
        return string(i, "claimed_by") == principal;
    }
    s.mine(string(i, "claimed_by")) || s.mine(string(i, "owner"))
}
fn active(s: &State, i: &Value) -> bool {
    ["claimed", "in_review"].contains(&string(i, "state"))
        && holder(s, i)
        && !(i["state"] == "claimed"
            && to_dt(string(i, "lease_until")).is_some_and(|t| t < s.config.now))
}

fn project(s: &State) -> Option<String> {
    let projects = crate::projects::load(&s.config.data.join("projects.yaml"));
    for p in &projects {
        if s.mine(string(p, "orchestrator"))
            || p["tracks"]
                .as_array()
                .into_iter()
                .flatten()
                .any(|v| s.mine(v.as_str().unwrap_or("")))
        {
            return Some(string(p, "id").into());
        }
    }
    crate::projects::of_path(&s.role_cwd.to_string_lossy(), &projects).or_else(|| {
        let mut count: Vec<(String, usize)> = vec![];
        for i in &s.held {
            let p = string(i, "project");
            if !p.is_empty() {
                if let Some((_, n)) = count.iter_mut().find(|(k, _)| k == p) {
                    *n += 1
                } else {
                    count.push((p.into(), 1));
                }
            }
        }
        count.sort_by_key(|a| std::cmp::Reverse(a.1));
        count.first().map(|(k, _)| k.clone())
    })
}

pub fn section(s: &mut State) -> Vec<String> {
    let path = &s.config.snapshot;
    let snap = crate::read_json(path, 64 << 20).unwrap_or(Value::Null);
    let list = if snap.is_object() {
        &snap["items"]
    } else {
        &snap
    };
    let Some(items) = list.as_array() else {
        return vec![
            "== Keeper".into(),
            format!("note: no readable keeper snapshot at {}", path.display()),
        ];
    };
    let items: Vec<Value> = items
        .iter()
        .filter(|i| i.is_object() && i["key"].is_string())
        .cloned()
        .collect();
    let generated = to_dt(string(&snap, "generated"))
        .or_else(|| mtime(path))
        .unwrap_or(s.config.now);
    let minutes = (s.config.now - generated).num_seconds().div_euclid(60);
    let mut out = vec![format!(
        "== Keeper snapshot {} old ({}){}",
        age(minutes * 60),
        stamp(generated),
        if minutes > 30 { " STALE" } else { "" }
    )];
    if s.role.is_empty() {
        out.push("note: no role, no keeper rows (pass --role)".into());
        return out;
    }
    // Preserve expired-lease rows as diagnostics, but never use them to
    // suppress LAPSED. They still explain why an old card disagrees.
    let mut held: Vec<_> = items
        .iter()
        .filter(|i| ["claimed", "in_review"].contains(&string(i, "state")) && holder(s, i))
        .cloned()
        .collect();
    held.sort_by(|a, b| string(b, "updated_at").cmp(string(a, "updated_at")));
    held.sort_by_key(|i| i["state"] != "claimed");
    s.held = held;
    out.push(format!(
        "held by {} (claimed, in_review): {}{}",
        s.role,
        s.held.len(),
        if s.held.is_empty() { " none" } else { "" }
    ));
    for i in s.held.iter().take(7) {
        let upd = to_dt(string(i, "updated_at"));
        let lease = to_dt(string(i, "lease_until"));
        let mut row = format!(
            "  {} {} {}",
            string(i, "key"),
            string(i, "state"),
            upd.map_or("?".into(), |t| age((s.config.now - t).num_seconds()))
        );
        if i["state"] == "claimed" {
            if let Some(t) = lease {
                row += &format!(
                    " lease {}",
                    if t < s.config.now {
                        "EXPIRED".into()
                    } else {
                        t.format("%H:%MZ").to_string()
                    }
                );
            }
        }
        row += &format!(" p{}", prio(i));
        for (k, tag) in [("claimed_by", "cb"), ("owner", "own")] {
            let v = string(i, k);
            if !v.is_empty() && !s.mine(v) {
                row += &format!(" {tag}:{v}");
            }
        }
        out.push(cut(&row, 160));
    }
    if s.held.len() > 7 {
        out.push(format!("  +{} more held", s.held.len() - 7));
    }
    let mut seen = std::collections::HashSet::new();
    for line in s.card_text.lines() {
        if let Some(c) = re(r"^\s*- claimed:\s*(.*)$").captures(line) {
            for key in re(r"\([^)]*(?:\)|$)")
                .replace_all(&c[1], "")
                .split(',')
                .map(str::trim)
            {
                if !re(r"^[\w-]+(?::[\w-]+)?$").is_match(key) || !seen.insert(key.to_owned()) {
                    continue;
                }
                let matches: Vec<_> = items
                    .iter()
                    .filter(|i| {
                        string(i, "key") == key
                            || (!key.contains(':')
                                && string(i, "key").split(':').next_back() == Some(key))
                    })
                    .collect();
                if !matches.iter().any(|i| active(s, i)) {
                    let state = matches
                        .first()
                        .map_or("unknown", |i| i["state"].as_str().unwrap_or("unknown"));
                    out.push(format!("LAPSED {key} state {state}: card says claimed, keeper does not; reclaim or fix the card"));
                }
            }
        }
    }
    if let Some(project) = project(s) {
        let (mut ready, n_open) = ready_items(&items, &snap["aliases"], &project);
        ready.sort_by_key(|i| {
            (
                -prio(i),
                if s.mine(string(i, "owner")) {
                    0
                } else if !truth(&i["owner"]) {
                    1
                } else {
                    2
                },
                string(i, "key").to_owned(),
            )
        });
        out.push(if ready.is_empty() {
            format!("ready in {project}: none ({n_open} open)")
        } else {
            format!(
                "ready in {project} (top {} of {}, {n_open} open):",
                ready.len().min(10),
                ready.len()
            )
        });
        for i in ready.iter().take(10) {
            let owner = string(i, "owner");
            let tag = if !owner.is_empty() && !s.mine(owner) {
                format!(" own:{owner}")
            } else {
                String::new()
            };
            out.push(cut(
                &format!(
                    "  p{} {}{tag} {}",
                    prio(i),
                    string(i, "key"),
                    string(i, "title")
                ),
                160,
            ));
        }
    } else {
        out.push(format!(
            "note: no project known for {}; no ready list",
            s.role
        ));
    }
    if s.role == "routing" {
        let mut ops: Vec<_> = items
            .iter()
            .filter(|i| i["kind"] == "operator_task" && i["state"] == "open")
            .collect();
        ops.sort_by(|a, b| string(b, "updated_at").cmp(string(a, "updated_at")));
        ops.sort_by_key(|i| -prio(i));
        out.push(format!(
            "open operator_task: {}{}",
            ops.len(),
            if ops.is_empty() { " none" } else { "" }
        ));
        for i in ops.iter().take(5) {
            out.push(cut(
                &format!("  p{} {} {}", prio(i), string(i, "key"), string(i, "title")),
                160,
            ));
        }
        if ops.len() > 5 {
            out.push(format!("  +{} more", ops.len() - 5));
        }
        out.push(format!(
            "operator answers waiting: {}",
            items
                .iter()
                .filter(|i| i["state"] == "open" && i["wait"] == "answer")
                .count()
        ));
    }
    out
}
