//! `plangraph land-train`: build a train from READY parts, verify one, and report left-out parts.
use super::build::{self, BuildInput};
use super::main_gh;
use super::main_state::State;
use super::train::{require_open_main, Manifest, PartIn};
use std::path::{Path, PathBuf};

const USAGE: &str = "usage:\n  plangraph land-train build --repo DIR --base SHA --name NAME --parts FILE [--attributes FILE] [--cap N] [--out DIR] [--branch NAME]\n  plangraph land-train verify --repo DIR --manifest FILE --parts FILE\n\
parts FILE: JSON array of {pr, head, parent: {pr, head} | null} in selection order (the registrar's READY entries)";

pub struct Build {
    pub repo: PathBuf,
    pub base: String,
    pub name: String,
    pub parts: Vec<PartIn>,
    pub attributes: String,
    pub cap: Option<usize>,
    pub out: PathBuf,
    pub branch: String,
}

/// Builds, publishes and verifies one train. Nothing is built while main is red or pending.
pub fn build_train(b: &Build, main: &State) -> Result<Vec<String>, String> {
    require_open_main(main)
        .map_err(|word| format!("{word}: no train is built while main is not green"))?;
    let outcome = build::build(&BuildInput {
        repo: &b.repo,
        build: b.name.clone(),
        base: b.base.clone(),
        parts: b.parts.clone(),
        attributes: b.attributes.clone(),
        cap: b.cap,
    })?;
    std::fs::create_dir_all(&b.out).map_err(|e| e.to_string())?;
    if let Some(m) = &outcome.manifest {
        build::publish(&b.repo, &b.branch, m)?;
        build::verify_manifest(&b.repo, m, &b.parts)?;
        write(&b.out.join(format!("{}.manifest.json", b.name)), m)?;
    }
    build::verify_result(&b.repo, &b.base, &outcome.result, outcome.manifest.as_ref())?;
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
    Ok(lines)
}

fn write(path: &Path, value: &impl serde::Serialize) -> Result<(), String> {
    std::fs::write(
        path,
        serde_json::to_vec_pretty(value).map_err(|e| e.to_string())?,
    )
    .map_err(|e| e.to_string())
}

fn read<T: serde::de::DeserializeOwned>(path: &str) -> Result<T, String> {
    serde_json::from_slice(&std::fs::read(path).map_err(|e| format!("{path}: {e}"))?)
        .map_err(|e| format!("{path}: {e}"))
}

pub fn run(home: &Path, args: &[String]) -> i32 {
    match run_inner(home, args) {
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

fn run_inner(home: &Path, args: &[String]) -> Result<Vec<String>, String> {
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
    let parts: Vec<PartIn> = read(&need("parts")?)?;
    match cmd.as_str() {
        "build" => {
            let (repo, ledger) = main_gh::repo_and_ledger(home, None, "main")?;
            let matrix = main_gh::matrix_from_toml(main_gh::MATRIX)?;
            let answer = main_gh::answer_for(repo, "main", ledger, &matrix);
            let name = need("name")?;
            build_train(
                &Build {
                    repo: PathBuf::from(need("repo")?),
                    base: need("base")?,
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
                    out: PathBuf::from(flags.get("out").cloned().unwrap_or_else(|| ".".into())),
                    branch: flags
                        .get("branch")
                        .cloned()
                        .unwrap_or_else(|| format!("land/build/{name}")),
                    name,
                },
                &answer.state,
            )
        }
        "verify" => {
            let manifest: Manifest = read(&need("manifest")?)?;
            let repo = PathBuf::from(need("repo")?);
            build::verify_manifest(&repo, &manifest, &parts)?;
            Ok(vec![format!(
                "train {} matches its manifest",
                manifest.build
            )])
        }
        _ => Err(USAGE.into()),
    }
}
