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

/// Run records of the spawner state directory, the remote lane branches of a checkout and
/// the open lanes Keeper reports (a lane another host pushed before this checkout fetched).
pub struct Observed {
    pub state: PathBuf,
    pub repo: PathBuf,
    pub keeper: Option<crate::lane_keeper::Fetch>,
}

impl Observed {
    /// The production source: Keeper is read when `PG_URL` and `PG_TOKEN_FILE` name it.
    pub fn new(state: PathBuf, repo: PathBuf) -> Self {
        Self {
            state,
            repo,
            keeper: crate::lane_keeper::from_env(),
        }
    }
}

impl LaneSource for Observed {
    fn lanes(&self, keys: &[String]) -> Vec<Lane> {
        let mut lanes = run_lanes(&live_runs(&self.state));
        lanes.extend(branch_lanes(&self.repo, keys));
        if let Some(fetch) = &self.keeper {
            lanes.extend(crate::lane_keeper::lanes(
                fetch,
                keys,
                head_branch(&self.repo).as_deref(),
            ));
        }
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
/// session recorded as `item_keys` (the same field batch cards use). Review and orchestration runs only read an item.
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
            for key in record["item_keys"].as_array().into_iter().flatten() {
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

fn git_output(repo: &Path, args: &[&str]) -> Option<String> {
    Command::new("git")
        .arg("-C")
        .arg(repo)
        .args(args)
        .output()
        .ok()
        .filter(|out| out.status.success())
        .map(|out| String::from_utf8_lossy(&out.stdout).into_owned())
}

/// The branch a checkout is on; none when detached or not a repository.
fn head_branch(repo: &Path) -> Option<String> {
    git_output(repo, &["symbolic-ref", "--short", "-q", "HEAD"]).map(|name| name.trim().to_owned())
}

/// Remote branches that carry a key's slug and are not merged into `origin/main`.
/// The branch the checkout itself is on is its own lane, never a collision.
pub fn branch_lanes(repo: &Path, keys: &[String]) -> Vec<Lane> {
    let git = |args: &[&str]| git_output(repo, args);
    let Some(refs) = git(&[
        "for-each-ref",
        "--no-merged=origin/main",
        "--format=%(refname:short)",
        "refs/remotes/origin",
    ]) else {
        return Vec::new();
    };
    let own = head_branch(repo);
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
/// card names, against the observed lanes. `override_reason` comes from explicit launch
/// metadata only (the typed input or the card), never from the brief text. Returns the
/// override reason that admitted a deliberate second lane and the keys admitted.
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
    let lanes = if keys.is_empty() {
        Vec::new()
    } else {
        source.lanes(&keys)
    };
    refuse_held(&keys, &lanes, override_reason).map(|reason| (reason, keys))
}

/// What a launch was admitted with: its keys, and the reason of a deliberate second lane.
#[derive(Debug, PartialEq, Eq)]
pub struct Launched {
    pub keys: Vec<String>,
    pub lane_override: Option<String>,
}

/// The one admission of a writer launch (native dispatch, the spawner CLI, the feeder): render
/// the prompt, take the override from `explicit` or the typed input, and check the keys of the
/// brief and of the card (`extra`) against the live runs, the remote lane branches of `repo`
/// and the Keeper's open lanes.
pub fn admit_launch(
    prompt: &str,
    extra: &[String],
    state: &Path,
    repo: &Path,
    explicit: Option<&str>,
) -> Result<Launched, String> {
    let rendered = crate::batch_writer_brief::render_input(prompt)?;
    let observed = Observed::new(state.to_owned(), repo.to_owned());
    let reason = explicit.or(rendered.lane_override.as_deref());
    admit(&rendered.brief, extra, &observed, reason).map(|(lane_override, keys)| Launched {
        keys,
        lane_override,
    })
}

/// `pg-spawnd lane-check [--state DIR] --repo DIR [--item KEY]... [--override REASON]` with the
/// prompt on stdin: prints `{"item_keys":[..],"lane_override":..}` and exits 0 when the launch
/// is admitted; exits 2 with the refusal on stderr otherwise.
pub fn execute(
    args: Vec<String>,
    prompt: &mut dyn std::io::Read,
    out: &mut dyn std::io::Write,
) -> i32 {
    let (mut state, mut repo, mut explicit) = (None, None, None);
    let mut items = Vec::new();
    let mut it = args.into_iter();
    while let Some(flag) = it.next() {
        match (flag.as_str(), it.next()) {
            ("--state", Some(v)) => state = Some(PathBuf::from(v)),
            ("--repo", Some(v)) => repo = Some(PathBuf::from(v)),
            ("--item", Some(v)) => items.push(v),
            ("--override", Some(v)) => explicit = Some(v),
            _ => {
                eprintln!("lane-check: usage: [--state DIR] --repo DIR [--item KEY]... [--override REASON] < prompt");
                return 2;
            }
        }
    }
    let Some(repo) = repo else {
        eprintln!("lane-check: --repo is required");
        return 2;
    };
    let state = match state.map_or_else(
        || crate::state::resolve_state(None, |key| std::env::var_os(key)),
        Ok,
    ) {
        Ok(state) => state,
        Err(error) => {
            eprintln!("lane-check: {error}");
            return 2;
        }
    };
    let mut text = String::new();
    if let Err(error) = prompt.read_to_string(&mut text) {
        eprintln!("lane-check: prompt unreadable: {error}");
        return 2;
    }
    match admit_launch(&text, &items, &state, &repo, explicit.as_deref()) {
        Ok(admitted) => {
            let _ = writeln!(
                out,
                "{}",
                serde_json::json!({"item_keys":admitted.keys,"lane_override":admitted.lane_override})
            );
            0
        }
        Err(error) => {
            eprintln!("{error}");
            2
        }
    }
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
    fn an_override_line_in_the_brief_text_is_not_an_override() {
        let brief = format!("{BRIEF}LANE_OVERRIDE: second lane on purpose\n");
        assert!(check_brief(&brief, &held("demo:ALPHA"), None).is_err());
    }

    #[test]
    fn keeper_lanes_refuse_a_key_before_its_branch_exists_locally() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path();
        git(repo, &["init", "-q", "-b", "main"]);
        git(repo, &["commit", "-q", "--allow-empty", "-m", "base"]);
        let show = json!({"work_exists":{"lanes":["lane/other-host-beta"]}});
        let observed = Observed {
            state: repo.join("state"),
            repo: repo.to_owned(),
            keeper: Some(Box::new(move |_| Ok(show.clone()))),
        };
        let error = check_brief(BRIEF, &observed, None).unwrap_err();
        assert!(
            error.starts_with(
                "lane_collision: demo:ALPHA is held by live lane lane/other-host-beta (keeper)"
            ),
            "{error}"
        );
        assert_eq!(
            check_brief(BRIEF, &observed, Some(" on purpose ")),
            Ok(Some("on purpose".into()))
        );
    }

    #[test]
    fn lane_check_verb_refuses_a_held_key_and_admits_an_override() {
        let dir = tempfile::tempdir().unwrap();
        let run = dir.path().join("runs/run-held");
        fs::create_dir_all(&run).unwrap();
        fs::write(
            run.join("record.json"),
            json!({"run_id":"run-held","status":"running","role":"implement",
                   "fleet_session_name":"writer-0673","item_key":"demo:ALPHA"})
            .to_string(),
        )
        .unwrap();
        let args = |extra: &[&str]| -> Vec<String> {
            [
                "--state",
                dir.path().to_str().unwrap(),
                "--repo",
                "/nonexistent",
                "--item",
                "demo:ALPHA",
            ]
            .into_iter()
            .chain(extra.iter().copied())
            .map(str::to_owned)
            .collect()
        };
        let mut out = Vec::new();
        assert_eq!(execute(args(&[]), &mut "brief".as_bytes(), &mut out), 2);
        assert!(out.is_empty());
        assert_eq!(
            execute(args(&["--override", "  "]), &mut "".as_bytes(), &mut out),
            2
        );
        assert_eq!(
            execute(
                args(&["--override", "fix lane"]),
                &mut "".as_bytes(),
                &mut out
            ),
            0
        );
        let admitted: Value = serde_json::from_slice(&out).unwrap();
        assert_eq!(admitted["lane_override"], "fix lane");
        assert_eq!(admitted["item_keys"], json!(["demo:ALPHA"]));
        // A key nobody holds needs no override.
        let free = [
            "--state",
            dir.path().to_str().unwrap(),
            "--repo",
            "/nonexistent",
            "--item",
            "demo:FREE",
        ];
        let mut out = Vec::new();
        let code = execute(
            free.iter().map(|s| s.to_string()).collect(),
            &mut "".as_bytes(),
            &mut out,
        );
        assert_eq!(code, 0);
    }

    #[test]
    fn live_runs_hold_their_item_key_brief_keys_and_recorded_items() {
        let runs = [
            json!({"run_id":"run-1","role":"implement","item_key":"demo:ONE"}),
            json!({"run_id":"run-2","role":"implement",
                   "manifest":{"brief":"### demo:TWO | t\nWhy\n"}}),
            json!({"run_id":"run-3","role":"implement","fleet_session_name":"writer-0009",
                   "item_keys":["demo:THREE"]}),
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
