//! Admission guard: one plan-item key has one live writer lane.
//!
//! The brief renderer, the Claude session launcher and native dispatch all refuse a key that
//! another live lane already holds, naming that lane. A deliberate second lane needs an
//! override with a nonblank reason; the reason is kept in the launch record.

use serde_json::Value;
use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
    process::Command,
};

/// A lane that holds item keys: a live spawner run or an unmerged remote branch.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Lane {
    pub name: String,
    pub keys: Vec<String>,
    pub source: &'static str,
}

/// Where the lanes of a set of keys come from.
pub trait LaneSource {
    fn lanes(&self, keys: &[String]) -> Vec<Lane>;
}

/// Run records of the spawner state directory plus the remote lane branches of a checkout.
pub struct Observed {
    pub state: PathBuf,
    pub repo: PathBuf,
}

impl LaneSource for Observed {
    fn lanes(&self, keys: &[String]) -> Vec<Lane> {
        let mut lanes = run_lanes(&live_runs(&self.state));
        lanes.extend(branch_lanes(&self.repo, keys));
        lanes
    }
}

/// The plan-item keys a writer brief names: `## key`, `### key |`, `Plan item: key`, `Item key: key`.
pub fn item_keys(brief: &str) -> Vec<String> {
    let assignment = regex::Regex::new(
        r"(?m)^(?:## |### |Plan item: |Item key: )([A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+)(?::|[ \t|]|\r?$)",
    )
    .expect("valid assignment pattern");
    let found: BTreeSet<String> = assignment
        .captures_iter(brief)
        .map(|capture| capture[1].to_owned())
        .collect();
    found.into_iter().collect()
}

/// The reason of a `LANE_OVERRIDE: <reason>` line of a rendered brief, blank when empty.
pub fn override_line(brief: &str) -> Option<String> {
    brief
        .lines()
        .find_map(|line| line.strip_prefix("LANE_OVERRIDE:"))
        .map(|reason| reason.trim().to_owned())
}

/// Records of the runs that are queued or running.
pub fn live_runs(state: &Path) -> Vec<Value> {
    let Ok(entries) = fs::read_dir(state.join("runs")) else {
        return Vec::new();
    };
    let mut paths: Vec<PathBuf> = entries
        .filter_map(Result::ok)
        .filter(|entry| entry.file_name().to_string_lossy().starts_with("run-"))
        .map(|entry| entry.path().join("record.json"))
        .collect();
    paths.sort();
    paths
        .iter()
        .filter_map(|path| serde_json::from_slice::<Value>(&fs::read(path).ok()?).ok())
        .filter(|record| {
            record["status"]
                .as_str()
                .is_some_and(|status| matches!(status, "queued" | "running"))
        })
        .collect()
}

/// A live writer run holds its `item_key`, the keys of its brief and the keys a fleet
/// session recorded as `brief_items`. Review and orchestration runs only read an item.
pub fn run_lanes(runs: &[Value]) -> Vec<Lane> {
    runs.iter()
        .filter(|record| record["role"] == "implement")
        .filter_map(|record| {
            let name = record["fleet_session_name"]
                .as_str()
                .or_else(|| record["run_id"].as_str())?;
            let mut keys = BTreeSet::new();
            let key_like = |text: &str| item_keys(&format!("Item key: {text}\n"));
            if let Some(key) = record["item_key"].as_str() {
                keys.extend(key_like(key));
            }
            for key in record["brief_items"].as_array().into_iter().flatten() {
                keys.extend(key.as_str().into_iter().flat_map(key_like));
            }
            if let Some(brief) = record["manifest"]["brief"].as_str() {
                keys.extend(item_keys(brief));
            }
            (!keys.is_empty()).then(|| Lane {
                name: name.to_owned(),
                keys: keys.into_iter().collect(),
                source: "run",
            })
        })
        .collect()
}

fn slug(key: &str) -> String {
    key.rsplit(':').next().unwrap_or(key).to_ascii_lowercase()
}

/// Remote branches that carry a key's slug and are not merged into `origin/main`.
/// The branch the checkout itself is on is its own lane, never a collision.
pub fn branch_lanes(repo: &Path, keys: &[String]) -> Vec<Lane> {
    let git = |args: &[&str]| {
        Command::new("git")
            .arg("-C")
            .arg(repo)
            .args(args)
            .output()
            .ok()
            .filter(|out| out.status.success())
            .map(|out| String::from_utf8_lossy(&out.stdout).into_owned())
    };
    let Some(refs) = git(&[
        "for-each-ref",
        "--no-merged=origin/main",
        "--format=%(refname:short)",
        "refs/remotes/origin",
    ]) else {
        return Vec::new();
    };
    let own = git(&["symbolic-ref", "--short", "-q", "HEAD"]).map(|name| name.trim().to_owned());
    refs.lines()
        .filter(|name| !name.ends_with("/HEAD") && *name != "origin/main")
        .filter(|name| own.as_deref() != name.strip_prefix("origin/"))
        .filter_map(|name| {
            let lower = name.to_ascii_lowercase();
            let held: Vec<String> = keys
                .iter()
                .filter(|key| lower.contains(&slug(key)))
                .cloned()
                .collect();
            (!held.is_empty()).then(|| Lane {
                name: name.to_owned(),
                keys: held,
                source: "branch",
            })
        })
        .collect()
}

/// Refuse every key of `keys` another live lane holds, naming the lane. With a nonblank
/// override reason a deliberate second lane passes and the reason is returned for the
/// launch record; a blank reason is refused.
pub fn refuse_held(
    keys: &[String],
    lanes: &[Lane],
    override_reason: Option<&str>,
) -> Result<Option<String>, String> {
    let reason = override_reason.map(str::trim);
    if reason == Some("") {
        return Err("lane_override requires a nonblank reason".into());
    }
    let held: Vec<String> = keys
        .iter()
        .filter_map(|key| {
            lanes
                .iter()
                .find(|lane| lane.keys.contains(key))
                .map(|lane| format!("{key} is held by live lane {} ({})", lane.name, lane.source))
        })
        .collect();
    if held.is_empty() {
        return Ok(None);
    }
    match reason {
        Some(reason) => Ok(Some(reason.to_owned())),
        None => Err(format!(
            "lane_collision: {}; a deliberate second lane needs a lane_override reason",
            held.join("; ")
        )),
    }
}

/// The admission check of a rendered or plain brief: its keys, and `extra` keys the launch
/// card names, against the observed lanes. Returns the override reason that admitted a
/// deliberate second lane and the keys admitted.
pub fn admit(
    brief: &str,
    extra: &[String],
    source: &dyn LaneSource,
    override_reason: Option<&str>,
) -> Result<(Option<String>, Vec<String>), String> {
    let mut keys = item_keys(brief);
    for key in extra {
        if !keys.contains(key) {
            keys.push(key.clone());
        }
    }
    let override_reason = override_reason
        .map(str::to_owned)
        .or_else(|| override_line(brief));
    let lanes = if keys.is_empty() {
        Vec::new()
    } else {
        source.lanes(&keys)
    };
    refuse_held(&keys, &lanes, override_reason.as_deref()).map(|reason| (reason, keys))
}

/// `admit` for a brief without card keys.
pub fn check_brief(
    brief: &str,
    source: &dyn LaneSource,
    override_reason: Option<&str>,
) -> Result<Option<String>, String> {
    admit(brief, &[], source, override_reason).map(|(reason, _)| reason)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    struct Fixed(Vec<Lane>);
    impl LaneSource for Fixed {
        fn lanes(&self, _: &[String]) -> Vec<Lane> {
            self.0.clone()
        }
    }

    fn held(key: &str) -> Fixed {
        Fixed(vec![Lane {
            name: "writer-0673".into(),
            keys: vec![key.into()],
            source: "run",
        }])
    }

    const BRIEF: &str = "### demo:ALPHA | title\nWhy: x\n\n### demo:BETA | title\nWhy: y\n";

    #[test]
    fn refuses_a_key_of_another_live_lane_and_names_the_lane() {
        let error = check_brief(BRIEF, &held("demo:BETA"), None).unwrap_err();
        assert!(error.starts_with("lane_collision: demo:BETA is held by live lane writer-0673"));
        assert!(!error.contains("demo:ALPHA"));
    }

    #[test]
    fn a_free_key_passes() {
        assert_eq!(check_brief(BRIEF, &held("demo:OTHER"), None), Ok(None));
    }

    #[test]
    fn override_with_a_reason_passes_and_is_returned() {
        let reason = check_brief(BRIEF, &held("demo:ALPHA"), Some("  fix lane  "));
        assert_eq!(reason, Ok(Some("fix lane".into())));
    }

    #[test]
    fn blank_override_is_refused_even_without_a_collision() {
        for blank in ["", "  \t"] {
            let error = check_brief(BRIEF, &held("demo:OTHER"), Some(blank)).unwrap_err();
            assert_eq!(error, "lane_override requires a nonblank reason");
        }
    }

    #[test]
    fn rendered_override_line_survives_the_forwarded_launch() {
        let brief = format!("{BRIEF}LANE_OVERRIDE: second lane on purpose\n");
        let reason = check_brief(&brief, &held("demo:ALPHA"), None);
        assert_eq!(reason, Ok(Some("second lane on purpose".into())));
        let blank = format!("{BRIEF}LANE_OVERRIDE:  \n");
        assert!(check_brief(&blank, &held("demo:ALPHA"), None).is_err());
    }

    #[test]
    fn live_runs_hold_their_item_key_brief_keys_and_recorded_items() {
        let runs = [
            json!({"run_id":"run-1","role":"implement","item_key":"demo:ONE"}),
            json!({"run_id":"run-2","role":"implement",
                   "manifest":{"brief":"### demo:TWO | t\nWhy\n"}}),
            json!({"run_id":"run-3","role":"implement","fleet_session_name":"writer-0009",
                   "brief_items":["demo:THREE"]}),
            json!({"run_id":"run-4","role":"implement","item_key":"bad key"}),
            json!({"run_id":"run-5","role":"review","item_key":"demo:ONE"}),
        ];
        let lanes = run_lanes(&runs);
        let by: Vec<_> = lanes
            .iter()
            .map(|l| (l.name.as_str(), l.keys.clone()))
            .collect();
        assert_eq!(
            by,
            [
                ("run-1", vec!["demo:ONE".to_string()]),
                ("run-2", vec!["demo:TWO".to_string()]),
                ("writer-0009", vec!["demo:THREE".to_string()]),
            ]
        );
    }

    #[test]
    fn only_queued_or_running_records_are_live() {
        let dir = tempfile::tempdir().unwrap();
        for (id, status) in [
            ("run-a", "running"),
            ("run-b", "completed"),
            ("run-c", "queued"),
        ] {
            let run = dir.path().join("runs").join(id);
            fs::create_dir_all(&run).unwrap();
            fs::write(
                run.join("record.json"),
                json!({"run_id":id,"status":status,"item_key":"demo:K"}).to_string(),
            )
            .unwrap();
        }
        let ids: Vec<_> = live_runs(dir.path())
            .iter()
            .map(|r| r["run_id"].as_str().unwrap().to_owned())
            .collect();
        assert_eq!(ids, ["run-a", "run-c"]);
    }

    fn git(dir: &Path, args: &[&str]) {
        let out = Command::new("git")
            .arg("-C")
            .arg(dir)
            .args(["-c", "user.name=t", "-c", "user.email=t@example.test"])
            .args(args)
            .output()
            .unwrap();
        assert!(out.status.success(), "{args:?}: {out:?}");
    }

    #[test]
    fn unmerged_remote_lane_branches_hold_the_key_slug() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path();
        git(repo, &["init", "-q", "-b", "main"]);
        git(repo, &["commit", "-q", "--allow-empty", "-m", "base"]);
        git(repo, &["update-ref", "refs/remotes/origin/main", "HEAD"]);
        git(repo, &["checkout", "-q", "-b", "lane/own-slug"]);
        git(repo, &["commit", "-q", "--allow-empty", "-m", "own"]);
        git(
            repo,
            &["update-ref", "refs/remotes/origin/lane/own-slug", "HEAD"],
        );
        git(
            repo,
            &["update-ref", "refs/remotes/origin/lane/fix-alpha-x", "HEAD"],
        );
        git(
            repo,
            &["update-ref", "refs/remotes/origin/lane/merged-beta", "main"],
        );
        let keys = [
            "demo:ALPHA-X".to_owned(),
            "demo:BETA".into(),
            "demo:OWN-SLUG".into(),
        ];
        let lanes = branch_lanes(repo, &keys);
        assert_eq!(
            lanes,
            [Lane {
                name: "origin/lane/fix-alpha-x".into(),
                keys: vec!["demo:ALPHA-X".into()],
                source: "branch",
            }]
        );
    }
}
