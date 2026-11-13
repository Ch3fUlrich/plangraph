use crate::digest_text::to_dt;
use crate::fleet_name::string;
use crate::handoff::re;
use crate::summary::{cut, project_of_session, split_key, Config};
use chrono::{DateTime, Utc};
use serde_json::{json, Value};
use std::collections::{BTreeMap, HashSet};
use std::path::Path;

pub struct Entry {
    pub project: String,
    pub item: String,
    pub reference: Option<String>,
    pub refs: Vec<String>,
    pub ts: DateTime<Utc>,
}
pub fn norm_ref(v: &Value) -> Option<String> {
    let s = if v.is_null() {
        String::new()
    } else {
        crate::digest_sources::py(v).trim().to_owned()
    };
    if s.is_empty() || s == "-" {
        None
    } else {
        Some(if re(r"^[0-9a-f]{7,40}$").is_match(&s) {
            s[..7].into()
        } else {
            s
        })
    }
}
pub fn entry(
    project: &str,
    item: &str,
    reference: &Value,
    ts: DateTime<Utc>,
    refs: Option<Vec<String>>,
) -> Entry {
    let reference = norm_ref(reference);
    Entry {
        project: project.into(),
        item: item.into(),
        refs: refs.unwrap_or_else(|| reference.iter().cloned().collect()),
        reference,
        ts,
    }
}
pub fn py_repr(s: &str) -> String {
    if s.contains('\'') && !s.contains('"') {
        return serde_json::to_string(s).unwrap();
    }
    format!(
        "'{}'",
        s.replace('\\', "\\\\")
            .replace('\'', "\\'")
            .replace('\n', "\\n")
            .replace('\r', "\\r")
            .replace('\t', "\\t")
    )
}
pub fn read_text(path: &Path) -> Option<String> {
    std::fs::read(path)
        .ok()
        .map(|b| String::from_utf8_lossy(&b).into_owned())
}
pub fn worklog(
    c: &Config,
    since: DateTime<Utc>,
    projects: &[Value],
    specs: &BTreeMap<String, Value>,
    known: &HashSet<String>,
) -> Vec<Entry> {
    if crate::paths::secret_at(&c.worklog, &c.home) {
        return vec![];
    }
    let mut out = vec![];
    for line in read_text(&c.worklog).unwrap_or_default().lines() {
        let Ok(r) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        if !r.is_object() || r.get("ts").is_none() || r.get("item").is_none() {
            continue;
        }
        let Some(t) = to_dt(string(&r, "ts")).filter(|t| *t >= since) else {
            continue;
        };
        let item = crate::digest_sources::py(&r["item"]);
        let (project, key) = split_key(&item, known);
        let project = project
            .or_else(|| project_of_session(string(&r, "session"), specs, projects))
            .or_else(|| crate::projects::of_path(string(&r, "src"), projects))
            .unwrap_or("other".into());
        out.push(entry(&project, &key, &r["ref"], t, None));
    }
    out
}
pub fn git(
    projects: &[Value],
    since: DateTime<Utc>,
    only: Option<&str>,
    runner: &dyn crate::digest::Runner,
) -> (Vec<Entry>, usize, Vec<String>) {
    let mut out = vec![];
    let mut ok = 0;
    let mut bad = vec![];
    let when = since.format("%Y-%m-%dT%H:%M:%S+00:00").to_string();
    for pr in projects {
        let path = string(pr, "path");
        let id = string(pr, "id");
        if only.is_some_and(|o| o != id)
            || (!path.starts_with('/') && !Path::new(path).is_absolute())
            || !Path::new(path).join(".git").exists()
        {
            continue;
        }
        let argv = crate::executor::strings(&[
            "-C",
            path,
            "log",
            "--first-parent",
            "main",
            &format!("--since={when}"),
            "--format=%H%x1f%cI%x1f%s",
            "--",
        ]);
        let result = runner.run("git", &argv, None);
        let output = match result {
            Ok(o) if o.code == 0 => o.stdout,
            _ => {
                bad.push(id.into());
                continue;
            }
        };
        ok += 1;
        for row in output.lines() {
            let mut parts = row.splitn(3, '\x1f');
            let sha = parts.next().unwrap_or("");
            let iso = parts.next().unwrap_or("");
            let subject = parts.next().unwrap_or("");
            let Some(t) = to_dt(iso).filter(|t| *t >= since) else {
                continue;
            };
            let key = re(r"\b(D-\d+|[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)\b")
                .captures(subject)
                .map_or_else(|| cut(subject, 28), |c| c[1].into());
            let sha = sha.chars().take(7).collect::<String>();
            let reference = re(r"(?:Merge pull request #|\(#|\bPR ?#)(\d+)")
                .captures(subject)
                .map_or_else(|| sha.clone(), |c| format!("PR#{}", &c[1]));
            out.push(entry(
                id,
                &key,
                &json!(reference),
                t,
                Some(vec![reference, sha]),
            ));
        }
    }
    (out, ok, bad)
}
pub fn keeper_refs(item: &Value) -> (Vec<String>, Vec<String>, Vec<String>) {
    let (mut shas, mut prs, mut decs) = (vec![], vec![], vec![]);
    for ev in item["evidence"].as_array().into_iter().flatten() {
        let r = &ev["ref"];
        if !r.is_object() {
            continue;
        }
        for key in ["commit", "sha", "merge_commit"] {
            if let Some(s) = r[key]
                .as_str()
                .filter(|s| re(r"^[0-9a-f]{7,40}$").is_match(s))
            {
                shas.push(s[..7].into());
            }
        }
        let pr = crate::digest_sources::py(&r["pr"]);
        if !pr.is_empty() && pr.chars().all(|c| c.is_ascii_digit()) {
            prs.push(format!("PR#{pr}"));
        }
        for key in ["id", "decision_id", "d_id"] {
            if let Some(s) = r[key].as_str().filter(|s| re(r"^D-\d+$").is_match(s)) {
                decs.push(s.into());
            }
        }
    }
    (shas, prs, decs)
}
pub struct Keeper {
    pub done: Vec<Entry>,
    pub projects: HashSet<String>,
    pub generated: Option<DateTime<Utc>>,
    pub note: Option<String>,
}
pub fn keeper(c: &Config, projects: &[Value], since: DateTime<Utc>) -> Keeper {
    let path = c.keeper.clone().or_else(|| {
        projects
            .iter()
            .find(|pr| {
                string(pr, "id") == "plangraph"
                    && (string(pr, "path").starts_with('/')
                        || Path::new(string(pr, "path")).is_absolute())
            })
            .map(|pr| Path::new(string(pr, "path")).join("out/plan-cdn.html"))
    });
    let mut res = Keeper {
        done: vec![],
        projects: HashSet::new(),
        generated: None,
        note: None,
    };
    let skip = |why: &str| {
        cut(&format!("note: keeper skipped, {why} (kg-export.json has no timestamps; the live snapshot needs router.token)"),200)
    };
    let Some(path) = path else {
        res.note = Some(skip("no plangraph path in projects.yaml"));
        return res;
    };
    if !path.is_file() {
        res.note = Some(skip(&format!("no export file at {}", path.display())));
        return res;
    }
    if crate::paths::secret_at(&path, &c.home) {
        res.note = Some(skip("export path looks like a secret file"));
        return res;
    }
    let text = read_text(&path).unwrap_or_default();
    let payload = if text.trim_start().starts_with(['{', '[']) {
        Some(text.as_str())
    } else {
        text.find("id=\"snapshot-data\"")
            .and_then(|i| text[i..].find('>').map(|n| i + n + 1))
            .and_then(|start| {
                text[start..]
                    .find("</script>")
                    .map(|n| &text[start..start + n])
            })
    }
    .and_then(|s| serde_json::from_str::<Value>(s).ok());
    let Some(payload) = payload else {
        res.note = Some(skip(&format!(
            "export {} has no readable snapshot",
            path.display()
        )));
        return res;
    };
    res.generated = to_dt(string(&payload, "generated"));
    let items = if payload.is_object() {
        &payload["items"]
    } else {
        &payload
    };
    let Some(items) = items.as_array() else {
        res.note = Some(skip(&format!(
            "export {} has no readable snapshot",
            path.display()
        )));
        return res;
    };
    for it in items.iter().filter(|i| i.is_object()) {
        let proj = it["project"]
            .as_str()
            .filter(|s| !s.is_empty())
            .unwrap_or("other");
        res.projects.insert(proj.into());
        let t = to_dt(
            it["done_at"]
                .as_str()
                .filter(|s| !s.is_empty())
                .unwrap_or(string(it, "updated_at")),
        );
        let Some(t) = t.filter(|t| {
            *t >= since && it["state"] == "done" && c.project.as_deref().is_none_or(|p| p == proj)
        }) else {
            continue;
        };
        let (shas, prs, decs) = keeper_refs(it);
        let reference = prs.first().or(shas.first()).or(decs.first()).cloned();
        let refs = shas.into_iter().chain(prs).collect();
        let id = it["id"]
            .as_str()
            .filter(|s| !s.is_empty())
            .or(it["key"].as_str().filter(|s| !s.is_empty()))
            .unwrap_or("?");
        res.done
            .push(entry(proj, id, &json!(reference), t, Some(refs)));
    }
    if let Some(t) = res.generated.filter(|t| *t < since) {
        res.note = Some(format!(
            "note: keeper export is from {}, before the window",
            t.format("%m-%dT%H:%MZ")
        ));
    }
    res
}
