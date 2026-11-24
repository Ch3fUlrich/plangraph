//! `plangraph land-train`: build a train from READY parts, verify one, and report left-out parts.
use super::build::{self, BuildInput, BuildResult};
use super::git::POLICY_VERSION;
use super::main_gh;
use super::main_state::State;
use super::resolve::{self, Adopted, Append, Outcome, Refusal};
use super::train::{check_freeze, require_open_main, BuildOpen, Freeze, Manifest, PartIn};
use std::path::{Path, PathBuf};
use std::process::Command;

const USAGE: &str = "usage:\n  plangraph land-train build --repo DIR --base SHA --name NAME --parts FILE --grant-digest D --snapshot-position N [--attributes FILE] [--cap N] [--resolution-log FILE] [--protected PREFIXES] [--out DIR] [--branch NAME]\n  plangraph land-train push --repo DIR --branch land/train-NAME\n  plangraph land-train verify|review|admit --repo DIR --manifest FILE --parts FILE --open FILE --freeze FILE --result FILE [--resolution-log FILE] [--protected PREFIXES] [--delta-out FILE]\n  review requires --item KEY; admit requires --item KEY --review-run ID\n\
  plangraph land-train check-chain --repo DIR --head SHA --heads FILE\n\
  plangraph land-train drive --repo DIR --trains FILE --out DIR --grant-digest D --snapshot-position N [--protected PREFIXES]\n  plangraph land-train plan --main SHA --trains FILE | combine --manifests A,B,... --out FILE | remerge --repo DIR --manifest FILE --main SHA --parts FILE --out FILE [--branch NAME] [--resolution-log FILE] [--protected PREFIXES]\n\
  plangraph land-train deploy-plan --repo DIR --manifest FILE --authorized-hosts A,B --probe-cmd CMD [--declared FILE] [--blockers-out FILE]\n\
  plangraph land-train go-verify --evidence FILE\n\
parts FILE: JSON array of {pr, head, parent: {pr, head} | null, receipt: {run_id, head}} in selection order (the registrar's READY entries)\n\
resolution log FILE: {\"writers\": {build: writer}, \"log\": [[append, result], ...]}; adopted results come from its replay";

#[derive(Clone)]
pub struct Build {
    pub repo: PathBuf,
    pub base: String,
    pub name: String,
    pub parts: Vec<PartIn>,
    pub attributes: String,
    pub cap: Option<usize>,
    pub out: PathBuf,
    pub branch: String,
    pub resolutions: Vec<Adopted>,
    pub protected: Vec<String>,
    /// `land_build_open`: policy version, base, grant digest and the snapshot position, fixed before building.
    pub open: BuildOpen,
}

/// The artifacts of one proof must belong together: the build record, the freeze, the manifest and the
/// result carry the same build, base and policy version (before any replay is trusted).
pub fn bind_artifacts(
    open: &BuildOpen,
    freeze: &Freeze,
    manifest: &Manifest,
    result: &BuildResult,
) -> Result<(), String> {
    let same = open.build == manifest.build
        && open.build == result.build
        && open.base == manifest.base
        && freeze.base == manifest.base
        && open.policy_version == POLICY_VERSION
        && manifest.policy.version == POLICY_VERSION
        && freeze.policy_version == POLICY_VERSION;
    if same {
        Ok(())
    } else {
        Err("artifacts of different builds, bases or policies: the open record, freeze, manifest and result must match".into())
    }
}

/// The tip of main that was read must be the base the train is built on.
pub fn require_base_is_tip(tip: &str, base: &str) -> Result<(), String> {
    if tip == base {
        Ok(())
    } else {
        Err(format!(
            "stale_base: main's tip is {tip}, the train base is {base}"
        ))
    }
}

/// Builds, verifies and publishes one train. Nothing is built while main is red or pending, or when the
/// tip that was read is not the base; the build record is written before the first merge.
pub fn build_train(b: &Build, main: &State, tip: &str) -> Result<Vec<String>, String> {
    require_open_main(main)
        .map_err(|word| format!("{word}: no train is built while main is not green"))?;
    require_base_is_tip(tip, &b.base)?;
    if b.open.policy_version != POLICY_VERSION || b.open.base != b.base || b.open.build != b.name {
        return Err("the build record does not match this build".into());
    }
    std::fs::create_dir_all(&b.out).map_err(|e| e.to_string())?;
    write(&b.out.join(format!("{}.open.json", b.name)), &b.open)?;
    let outcome = build::build(&BuildInput {
        repo: &b.repo,
        build: b.name.clone(),
        base: b.base.clone(),
        parts: b.parts.clone(),
        attributes: b.attributes.clone(),
        cap: b.cap,
        resolutions: b.resolutions.clone(),
        protected: b.protected.clone(),
    })?;
    // The proof comes first; the branch is published only for a train that passed it.
    if let Some(m) = &outcome.manifest {
        build::verify_manifest_with(&b.repo, m, &b.parts, &b.resolutions, &b.protected)?;
    }
    build::verify_result(&b.repo, &b.base, &outcome.result, outcome.manifest.as_ref())?;
    let warnings = if let Some(m) = &outcome.manifest {
        let warnings = build::shared_part_paths(&b.repo, m)?;
        build::publish(&b.repo, &b.branch, m)?;
        write(&b.out.join(format!("{}.manifest.json", b.name)), m)?;
        warnings
    } else {
        Vec::new()
    };
    write(
        &b.out.join(format!("{}.result.json", b.name)),
        &outcome.result,
    )?;
    let mut lines = vec![format!(
        "train {}: {} merged, {} left out",
        b.name,
        outcome.result.merged.len(),
        outcome.result.left_out.len()
    )];
    if let Some(m) = &outcome.manifest {
        lines.push(format!("branch {} head {}", b.branch, m.train_sha));
    }
    for l in &outcome.result.left_out {
        lines.push(format!("left out #{} ({}) {:?}", l.pr, l.head, l.reason));
    }
    lines.extend(warnings);
    Ok(lines)
}

#[derive(serde::Deserialize)]
pub struct ResolutionLog {
    pub writers: std::collections::BTreeMap<String, String>,
    pub log: Vec<(Append, Result<Outcome, Refusal>)>,
}

/// The adopted results of `build`, from the replay of the recorded resolution log (never caller-supplied).
pub fn adopted_from_log(path: &str, build: &str) -> Result<Vec<Adopted>, String> {
    let log: ResolutionLog = read(path)?;
    let ledger = resolve::replay(log.writers, &log.log)
        .map_err(|at| format!("resolution log refused at append {at}"))?;
    Ok(ledger.adopted(build))
}

/// The state and tip of main for the repository this checkout belongs to.
pub(super) fn main_answer(
    home: &Path,
    repo_dir: &Path,
) -> Result<super::main_state::Answer, String> {
    let origin = super::git::out(repo_dir, &["remote", "get-url", "origin"])?;
    let slug = crate::ci_watch::github::repository(&origin)?;
    let (repo, ledger) = main_gh::repo_and_ledger(home, Some(&slug), "main")?;
    let matrix = main_gh::matrix_from_toml(main_gh::MATRIX)?;
    Ok(main_gh::answer_for(repo, "main", ledger, &matrix))
}

pub(super) fn write(path: &Path, value: &impl serde::Serialize) -> Result<(), String> {
    std::fs::write(
        path,
        serde_json::to_vec_pretty(value).map_err(|e| e.to_string())?,
    )
    .map_err(|e| e.to_string())
}

pub(super) fn read<T: serde::de::DeserializeOwned>(path: &str) -> Result<T, String> {
    serde_json::from_slice(&std::fs::read(path).map_err(|e| format!("{path}: {e}"))?)
        .map_err(|e| format!("{path}: {e}"))
}

pub fn run(home: &Path, args: &[String]) -> i32 {
    let python = || crate::base_fresh::python_command(None);
    match run_inner(home, args, &python) {
        Ok(lines) => {
            lines.iter().for_each(|l| println!("{l}"));
            0
        }
        Err(e) => {
            eprintln!("{e}");
            if e.starts_with("usage") {
                2
            } else {
                3
            }
        }
    }
}

pub(super) fn run_inner(
    home: &Path,
    args: &[String],
    python: &dyn Fn() -> Command,
) -> Result<Vec<String>, String> {
    let (cmd, rest) = args.split_first().ok_or(USAGE)?;
    let mut flags = std::collections::BTreeMap::new();
    let mut it = rest.iter();
    while let Some(k) = it.next() {
        let key = k.strip_prefix("--").ok_or(USAGE)?;
        flags.insert(key.to_owned(), it.next().ok_or(USAGE)?.clone());
    }
    let need = |k: &str| {
        flags
            .get(k)
            .cloned()
            .ok_or_else(|| format!("{USAGE}\nmissing --{k}"))
    };
    if cmd == "push" {
        let repo = PathBuf::from(need("repo")?);
        let branch = need("branch")?;
        super::push::run(&repo, &branch)?;
        return Ok(vec![format!("pushed {branch}")]);
    }
    if cmd == "deploy-plan" {
        return super::deploy::run_cli(&flags);
    }
    if cmd == "go-verify" {
        return super::go_verify::run_cli(&flags);
    }
    if cmd == "check-chain" {
        super::chain::check_push(
            Path::new(&need("repo")?),
            &need("head")?,
            Path::new(&need("heads")?),
        )?;
        return Ok(Vec::new());
    }
    if let "plan" | "combine" | "remerge" | "drive" = cmd.as_str() {
        return super::driver_cli::run(home, cmd, &flags);
    }
    let parts: Vec<PartIn> = read(&need("parts")?)?;
    let protected: Vec<String> = flags
        .get("protected")
        .map(|p| p.split(',').map(str::to_owned).collect())
        .unwrap_or_default();
    match cmd.as_str() {
        "build" => {
            let repo_dir = PathBuf::from(need("repo")?);
            let answer = main_answer(home, &repo_dir)?;
            let name = need("name")?;
            let base = need("base")?;
            let adopted = match flags.get("resolution-log") {
                Some(p) => adopted_from_log(p, &name)?,
                None => vec![],
            };
            build_train(
                &Build {
                    repo: repo_dir,
                    base: base.clone(),
                    parts,
                    attributes: flags
                        .get("attributes")
                        .map(|p| std::fs::read_to_string(p).map_err(|e| e.to_string()))
                        .transpose()?
                        .unwrap_or_default(),
                    cap: flags
                        .get("cap")
                        .map(|c| c.parse().map_err(|_| "cap must be a number".to_owned()))
                        .transpose()?,
                    resolutions: adopted,
                    protected,
                    open: BuildOpen {
                        build: name.clone(),
                        policy_version: POLICY_VERSION.into(),
                        base,
                        grant_digest: need("grant-digest")?,
                        snapshot_position: need("snapshot-position")?
                            .parse()
                            .map_err(|_| "snapshot-position must be a number".to_owned())?,
                    },
                    out: PathBuf::from(flags.get("out").cloned().unwrap_or_else(|| ".".into())),
                    branch: flags
                        .get("branch")
                        .cloned()
                        .unwrap_or_else(|| format!("land/train-{name}")),
                    name,
                },
                &answer.state,
                &answer.tip,
            )
        }
        "verify" | "review" | "admit" => {
            let manifest: Manifest = read(&need("manifest")?)?;
            let repo = PathBuf::from(need("repo")?);
            let open: BuildOpen = read(&need("open")?)?;
            let freeze: Freeze = read(&need("freeze")?)?;
            let result: BuildResult = read(&need("result")?)?;
            let adopted = match flags.get("resolution-log") {
                Some(p) => adopted_from_log(p, &manifest.build)?,
                None => vec![],
            };
            bind_artifacts(&open, &freeze, &manifest, &result)?;
            check_freeze(&open, &result.snapshot, &freeze)
                .map_err(|e| format!("freeze refused: {e:?}"))?;
            if freeze.selection != parts {
                return Err(
                    "freeze refused: the selection is not the one recomputed by the verifier"
                        .into(),
                );
            }
            let delta =
                build::verify_manifest_with(&repo, &manifest, &parts, &adopted, &protected)?;
            build::verify_result(&repo, &manifest.base, &result, Some(&manifest))?;
            let mut lines = vec![format!(
                "train {} matches its manifest, result and freeze",
                manifest.build
            )];
            if flags.contains_key("delta-out") || cmd != "verify" {
                let packet = super::delta::ReviewPacket::new(&delta, &repo, &manifest, python)?;
                if cmd == "admit" {
                    let config = std::env::var_os("XDG_CONFIG_HOME")
                        .map(PathBuf::from)
                        .unwrap_or_else(|| home.join(".config"))
                        .join("plangraph/keeper.json");
                    packet.require_review(&config, &need("item")?, &need("review-run")?, python)?;
                }
                if cmd == "review" {
                    let run_id = packet.dispatch(&repo, &need("item")?, python)?;
                    lines.push(format!("train delta review run {run_id}"));
                }
                if let Some(path) = flags.get("delta-out") {
                    let packet = serde_json::to_vec_pretty(&packet).map_err(|e| e.to_string())?;
                    pg_worker_core::run::record_io::atomic_write_private(Path::new(path), &packet)
                        .map_err(|e| e.to_string())?;
                }
            }
            Ok(lines)
        }
        _ => Err(USAGE.into()),
    }
}
