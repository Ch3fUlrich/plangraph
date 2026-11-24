//! Admission guard: one plan-item key has one live writer lane on a host.
//!
//! A key moves through three states on one host: free, reserved (an owner token and a time
//! under the flock of `<state>/lane-reservations/.lock`) and held by a run record (status
//! queued or running, role implement, the key in `item_key`, `item_keys` or the brief). The
//! check and the reservation are one step under that lock, taken from the one state path the
//! caller passes in. The reservation is released when `Launched` drops, so a refused or failed
//! launch leaves no claim; the run record that the launch registers takes over. A deliberate
//! second lane needs an override with a nonblank reason, which is kept in the launch record.

use serde_json::Value;
use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
    process::Command,
    time::{SystemTime, UNIX_EPOCH},
};

/// A lane that holds item keys: a live spawner run, a reservation or an unmerged remote branch.
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

/// Run records and reservations of the spawner state directory and the remote lane branches
/// of a checkout.
pub struct Observed {
    pub state: PathBuf,
    pub repo: PathBuf,
    /// The launch asking: its own reservations are not lanes of another launch.
    pub owner: String,
}

impl LaneSource for Observed {
    fn lanes(&self, keys: &[String]) -> Vec<Lane> {
        let mut lanes = run_lanes(&live_runs(&self.state));
        lanes.extend(crate::lane_reservation::lanes(
            &self.state,
            keys,
            &self.owner,
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map_or(0, |d| d.as_secs()),
        ));
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

/// The lowercase tokens of a name, split on the delimiters of branch names.
fn tokens(name: &str) -> Vec<String> {
    name.to_ascii_lowercase()
        .split(['/', '-', '_', '.'])
        .filter(|token| !token.is_empty())
        .map(str::to_owned)
        .collect()
}

/// A key's slug as tokens: the part after the project, so `demo:ALPHA-X` is `alpha`, `x`.
fn slug(key: &str) -> Vec<String> {
    tokens(key.rsplit(':').next().unwrap_or(key))
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

/// Remote branches that carry a key's slug as whole tokens and are not merged into
/// `origin/main`: `lane/fix-alpha-x` holds `demo:ALPHA-X` and `demo:FIX`, never `demo:FIXTURE`.
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
            let branch = tokens(name);
            let held: Vec<String> = keys
                .iter()
                .filter(|key| {
                    let slug = slug(key);
                    !slug.is_empty() && branch.windows(slug.len()).any(|part| part == slug)
                })
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

/// What a launch was admitted with: its keys and the reason of a deliberate second lane. While
/// it lives the keys stay reserved for this launch; dropping it releases them, so every refused
/// or failed launch path frees the keys and the run record the launch registered holds them.
#[derive(Debug)]
pub struct Launched {
    pub keys: Vec<String>,
    pub lane_override: Option<String>,
    _reservation: Option<Reservation>,
}

/// The reservation of an admitted launch; dropping it releases the keys of this owner.
#[derive(Debug)]
struct Reservation {
    state: PathBuf,
    keys: Vec<String>,
    owner: String,
}

impl Drop for Reservation {
    fn drop(&mut self) {
        // A lock that stays busy leaves the reservation to expire (the crash backstop).
        if let Ok(_lock) = crate::lane_reservation::lock(&self.state) {
            crate::lane_reservation::release(&self.state, &self.keys, &self.owner);
        }
    }
}

/// The most keys one launch holds: the run record keeps a batch of at most this many.
pub const MAX_KEYS: usize = 50;

/// One writer launch to admit.
pub struct LaunchRequest<'a> {
    pub prompt: &'a str,
    /// Keys the launch card names besides the brief's own.
    pub extra: &'a [String],
    /// The one state path of the entry point: lock, reservation and run lookup all use it.
    pub state: &'a Path,
    pub repo: &'a Path,
    /// The override reason of explicit launch metadata (the card's `lane_override_reason`).
    pub explicit: Option<&'a str>,
    /// Reserve the admitted keys for this launch; a check-only caller (a dry run) leaves them free.
    pub reserve: bool,
}

/// The one admission of a writer launch: render the prompt, take the override from `explicit`
/// or the typed input, and check the keys of the brief and of the card against the live runs,
/// the remote lane branches of `repo` and the reservations of other launches. With `reserve`
/// the check and the reservation are one step under the state's lock, so two concurrent launches
/// of a key cannot both be admitted.
pub fn admit_launch(request: &LaunchRequest) -> Result<Launched, String> {
    let rendered = crate::batch_writer_brief::render_input(request.prompt)?;
    let owner = crate::lane_reservation::new_owner();
    let observed = Observed {
        state: request.state.to_owned(),
        repo: request.repo.to_owned(),
        owner: owner.clone(),
    };
    let reason = request.explicit.or(rendered.lane_override.as_deref());
    let _lock = request
        .reserve
        .then(|| crate::lane_reservation::lock(request.state))
        .transpose()?;
    let (lane_override, keys) = admit(&rendered.brief, request.extra, &observed, reason)?;
    if keys.len() > MAX_KEYS {
        return Err(format!("a launch holds at most {MAX_KEYS} item keys"));
    }
    let reservation = if request.reserve {
        if let Err(error) = crate::lane_reservation::reserve(request.state, &keys, &owner) {
            // Still under the lock: drop what was written before the failure.
            crate::lane_reservation::release(request.state, &keys, &owner);
            return Err(format!("lane reservation: {error}"));
        }
        Some(Reservation {
            state: request.state.to_owned(),
            keys: keys.clone(),
            owner,
        })
    } else {
        None
    };
    Ok(Launched {
        keys,
        lane_override,
        _reservation: reservation,
    })
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

    fn check_brief(
        brief: &str,
        source: &dyn LaneSource,
        override_reason: Option<&str>,
    ) -> Result<Option<String>, String> {
        admit(brief, &[], source, override_reason).map(|(reason, _)| reason)
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

    #[test]
    fn branch_slug_matches_whole_tokens_only() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path();
        git(repo, &["init", "-q", "-b", "main"]);
        git(repo, &["commit", "-q", "--allow-empty", "-m", "base"]);
        git(repo, &["update-ref", "refs/remotes/origin/main", "HEAD"]);
        git(repo, &["checkout", "-q", "-b", "lane/mine"]);
        git(repo, &["commit", "-q", "--allow-empty", "-m", "own"]);
        for branch in [
            "lane/fixture-a",
            "lane/prefix-1",
            "lane/fix-b",
            "lane/a.fix_c",
        ] {
            git(
                repo,
                &[
                    "update-ref",
                    &format!("refs/remotes/origin/{branch}"),
                    "HEAD",
                ],
            );
        }
        let lanes = branch_lanes(repo, &["demo:FIX".to_owned()]);
        let mut names: Vec<_> = lanes.iter().map(|lane| lane.name.as_str()).collect();
        names.sort_unstable();
        assert_eq!(names, ["origin/lane/a.fix_c", "origin/lane/fix-b"]);
        // A slug of several tokens must appear in order and side by side.
        let keys = ["demo:ALPHA-X".to_owned()];
        git(
            repo,
            &["update-ref", "refs/remotes/origin/lane/x-alpha", "HEAD"],
        );
        assert!(branch_lanes(repo, &keys).is_empty());
    }

    fn request<'a>(state: &'a Path, extra: &'a [String]) -> LaunchRequest<'a> {
        LaunchRequest {
            prompt: "Brief\n",
            extra,
            state,
            repo: Path::new("/nonexistent"),
            explicit: None,
            reserve: true,
        }
    }

    fn reservation_files(state: &Path) -> usize {
        fs::read_dir(state.join("lane-reservations"))
            .into_iter()
            .flatten()
            .filter_map(Result::ok)
            .filter(|entry| entry.path().extension().is_some_and(|ext| ext == "json"))
            .count()
    }

    #[test]
    fn refused_launch_releases_reservation() {
        let dir = tempfile::tempdir().unwrap();
        let keys = ["demo:A".to_owned(), "demo:B".to_owned()];
        let first = admit_launch(&request(dir.path(), &keys)).unwrap();
        assert_eq!(reservation_files(dir.path()), 2);
        // A second launch is refused naming the reservation and reserves nothing new.
        let error = admit_launch(&request(dir.path(), &keys[1..])).unwrap_err();
        assert!(error.contains("held by live lane reservation"), "{error}");
        assert_eq!(reservation_files(dir.path()), 2);
        // The first launch fails after admission: dropping it frees the keys at once.
        drop(first);
        assert_eq!(reservation_files(dir.path()), 0);
        drop(admit_launch(&request(dir.path(), &keys[1..])).unwrap());
        assert_eq!(reservation_files(dir.path()), 0);
    }

    #[test]
    fn a_check_only_request_reserves_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let keys = ["demo:A".to_owned()];
        let mut check = request(dir.path(), &keys);
        check.reserve = false;
        let launched = admit_launch(&check).unwrap();
        assert_eq!(launched.keys, keys);
        assert_eq!(reservation_files(dir.path()), 0);
        assert!(admit_launch(&request(dir.path(), &keys)).is_ok());
    }

    #[test]
    fn two_concurrent_admissions_of_one_key_admit_one() {
        let dir = tempfile::tempdir().unwrap();
        let keys = ["demo:RACE".to_owned()];
        let barrier = std::sync::Barrier::new(8);
        let results: Vec<Result<Launched, String>> = std::thread::scope(|scope| {
            let workers: Vec<_> = (0..8)
                .map(|_| {
                    scope.spawn(|| {
                        barrier.wait();
                        admit_launch(&request(dir.path(), &keys))
                    })
                })
                .collect();
            workers.into_iter().map(|w| w.join().unwrap()).collect()
        });
        assert_eq!(
            results.iter().filter(|r| r.is_ok()).count(),
            1,
            "{results:?}"
        );
        for refused in results.iter().filter_map(|r| r.as_ref().err()) {
            assert!(
                refused.contains("demo:RACE is held by live lane"),
                "{refused}"
            );
        }
    }

    #[test]
    fn override_with_reason_is_recorded_and_blank_is_refused() {
        let dir = tempfile::tempdir().unwrap();
        let keys = ["demo:HELD".to_owned()];
        let first = admit_launch(&request(dir.path(), &keys)).unwrap();
        let mut second = request(dir.path(), &keys);
        second.explicit = Some("  fix lane  ");
        let launched = admit_launch(&second).unwrap();
        assert_eq!(launched.lane_override.as_deref(), Some("fix lane"));
        assert_eq!(launched.keys, keys);
        for blank in ["", "  \t"] {
            second.explicit = Some(blank);
            let error = admit_launch(&second).unwrap_err();
            assert_eq!(error, "lane_override requires a nonblank reason");
        }
        drop((first, launched));
    }
}
