//! `land-train plan|combine|remerge|drive`: the commands the parallel-trains driver runs between CI results.
use super::cli::{adopted_from_log, main_answer, read, write};
use super::combine::combine_parts;
use super::driver::{drive, Ctx, DriveTrain};
use super::parallel::{plan, Ci, TrainState};
use super::remerge::{publish_swap, remerge_verified};
use super::resolve::Adopted;
use super::train::{Manifest, PartIn};
use serde::Deserialize;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

/// One train of `drive --trains FILE`: its CI state and the files of its build.
#[derive(Deserialize)]
struct TrainFile {
    name: String,
    main_in: String,
    ci: Ci,
    manifest: String,
    parts: String,
    resolution_log: Option<String>,
}

fn protected(flags: &BTreeMap<String, String>) -> Vec<String> {
    flags
        .get("protected")
        .map(|p| p.split(',').map(str::to_owned).collect())
        .unwrap_or_default()
}

fn adopted(log: Option<&String>, manifest: &Manifest) -> Result<Vec<Adopted>, String> {
    log.map_or(Ok(vec![]), |p| adopted_from_log(p, &manifest.build))
}

pub(super) fn run(
    home: &Path,
    cmd: &str,
    flags: &BTreeMap<String, String>,
) -> Result<Vec<String>, String> {
    let need = |k: &str| {
        flags
            .get(k)
            .cloned()
            .ok_or_else(|| format!("missing --{k}"))
    };
    match cmd {
        "plan" => {
            let trains: Vec<TrainState> = read(&need("trains")?)?;
            plan(&need("main")?, &trains)
                .iter()
                .map(|s| serde_json::to_string(s).map_err(|e| e.to_string()))
                .collect()
        }
        "combine" => {
            let manifests = need("manifests")?
                .split(',')
                .map(read::<Manifest>)
                .collect::<Result<Vec<_>, _>>()?;
            let parts = combine_parts(&manifests)?;
            write(Path::new(&need("out")?), &parts)?;
            Ok(vec![format!(
                "combined {} trains into {} parts",
                manifests.len(),
                parts.len()
            )])
        }
        "drive" => {
            let repo = PathBuf::from(need("repo")?);
            let files: Vec<TrainFile> = read(&need("trains")?)?;
            let mut trains = Vec::new();
            for f in files {
                let manifest: Manifest = read(&f.manifest)?;
                trains.push(DriveTrain {
                    adopted: adopted(f.resolution_log.as_ref(), &manifest)?,
                    parts: read(&f.parts)?,
                    state: TrainState {
                        name: f.name,
                        main_in: f.main_in,
                        ci: f.ci,
                    },
                    manifest,
                });
            }
            let answer = main_answer(home, &repo)?;
            let out = PathBuf::from(need("out")?);
            std::fs::create_dir_all(&out).map_err(|e| e.to_string())?;
            let ctx = Ctx {
                repo: &repo,
                main_tip: &answer.tip,
                main: &answer.state,
                grant_digest: &need("grant-digest")?,
                snapshot_position: need("snapshot-position")?
                    .parse()
                    .map_err(|_| "snapshot-position must be a number".to_owned())?,
                out: &out,
                protected: &protected(flags),
            };
            drive(&ctx, &trains)?
                .iter()
                .map(|o| serde_json::to_string(o).map_err(|e| e.to_string()))
                .collect()
        }
        _ => {
            let repo = PathBuf::from(need("repo")?);
            let manifest: Manifest = read(&need("manifest")?)?;
            let parts: Vec<PartIn> = read(&need("parts")?)?;
            // The proof comes first, under the same resolutions and protected paths as `verify`; the
            // branch moves only for a train that passed it, and only from the head the manifest names.
            let next = remerge_verified(
                &repo,
                &manifest,
                &need("main")?,
                &parts,
                &adopted(flags.get("resolution-log"), &manifest)?,
                &protected(flags),
            )?;
            let branch = flags
                .get("branch")
                .cloned()
                .unwrap_or_else(|| format!("land/train-{}", next.build));
            publish_swap(&repo, &branch, &manifest, &next, Path::new(&need("out")?))?;
            Ok(vec![format!("branch {branch} head {}", next.train_sha)])
        }
    }
}
