//! `land-train plan|combine|remerge`: the commands the parallel-trains driver runs between CI results.
use super::build;
use super::cli::{read, write};
use super::combine::combine_parts;
use super::parallel::{plan, TrainState};
use super::remerge::remerge;
use super::train::{Manifest, PartIn};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

pub(super) fn run(cmd: &str, flags: &BTreeMap<String, String>) -> Result<Vec<String>, String> {
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
        _ => {
            let repo = PathBuf::from(need("repo")?);
            let manifest: Manifest = read(&need("manifest")?)?;
            let parts: Vec<PartIn> = read(&need("parts")?)?;
            let next = remerge(&repo, &manifest, &need("main")?)?;
            // The proof comes first; the branch moves only for a train that passed it.
            build::verify_manifest_with(&repo, &next, &parts, &[], &[])?;
            let branch = flags
                .get("branch")
                .cloned()
                .unwrap_or_else(|| format!("land/train-{}", next.build));
            build::publish(&repo, &branch, &next)?;
            write(Path::new(&need("out")?), &next)?;
            Ok(vec![format!("branch {branch} head {}", next.train_sha)])
        }
    }
}
