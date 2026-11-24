//! The parallel-trains driver (D-6774): runs the steps `parallel::plan` names on real trains. A remerge
//! moves the branch with the manifest, a combine and a rebuild after a drop go through the production
//! builder (`build_train`: main must be green and its tip the base). A landing is not done here: the GO
//! verifier binds the evidence to the head, so a remerged, combined or rebuilt head lands only after its
//! own full CI and its own delta review (the remerge-diff review for a remerge), never a part review.
use super::cli::{build_train, Build};
use super::git::POLICY_VERSION;
use super::main_state::State;
use super::parallel::{plan, Step, TrainState};
use super::remerge::{publish_swap, remerge_verified};
use super::resolve::Adopted;
use super::split::{Action, Session};
use super::train::{BuildOpen, Manifest, PartIn};
use serde::Serialize;
use std::path::Path;

pub struct DriveTrain {
    pub state: TrainState,
    pub manifest: Manifest,
    pub parts: Vec<PartIn>,
    /// The adopted resolutions of this train's build (from its resolution log).
    pub adopted: Vec<Adopted>,
}

pub struct Ctx<'a> {
    pub repo: &'a Path,
    pub main_tip: &'a str,
    pub main: &'a State,
    pub grant_digest: &'a str,
    pub snapshot_position: u64,
    pub out: &'a Path,
    pub protected: &'a [String],
}

#[derive(Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    /// Green and on live main: the GO gate lands it (`go-verify`); nothing is merged here.
    Land {
        train: String,
        head: String,
    },
    /// Main merged into the train; the new head needs one full CI and the remerge-diff review.
    Remerged {
        train: String,
        head: String,
    },
    /// One bigger train built on live main from several; it needs one full CI and one delta review.
    Combined {
        trains: Vec<String>,
        build: String,
        head: String,
    },
    /// The hinted parts left the train; the rest was rebuilt on live main when anything remained.
    Dropped {
        train: String,
        evicted: Vec<u64>,
        rebuilt: Option<(String, String)>,
    },
    Wait,
}

fn rebuild(
    ctx: &Ctx,
    name: &str,
    parts: Vec<PartIn>,
    attributes: &str,
    adopted: &[Adopted],
) -> Result<String, String> {
    let build = Build {
        repo: ctx.repo.to_owned(),
        base: ctx.main_tip.to_owned(),
        name: name.to_owned(),
        parts,
        attributes: attributes.to_owned(),
        cap: None,
        out: ctx.out.to_owned(),
        branch: format!("land/train-{name}"),
        resolutions: adopted.to_vec(),
        protected: ctx.protected.to_vec(),
        open: BuildOpen {
            build: name.to_owned(),
            policy_version: POLICY_VERSION.into(),
            base: ctx.main_tip.to_owned(),
            grant_digest: ctx.grant_digest.to_owned(),
            snapshot_position: ctx.snapshot_position,
        },
    };
    build_train(&build, ctx.main, ctx.main_tip)?;
    let manifest: Manifest = super::cli::read(
        &ctx.out
            .join(format!("{name}.manifest.json"))
            .to_string_lossy(),
    )
    .map_err(|_| format!("{name}: every part was left out, no train was built"))?;
    Ok(manifest.train_sha)
}

fn find<'a>(trains: &'a [DriveTrain], name: &str) -> Result<&'a DriveTrain, String> {
    trains
        .iter()
        .find(|t| t.state.name == name)
        .ok_or_else(|| format!("drive: no train {name}"))
}

/// Runs the next steps for `trains` and reports what each did.
pub fn drive(ctx: &Ctx, trains: &[DriveTrain]) -> Result<Vec<Outcome>, String> {
    let states: Vec<TrainState> = trains.iter().map(|t| t.state.clone()).collect();
    let mut outcomes = Vec::new();
    for step in plan(ctx.main_tip, &states) {
        outcomes.push(match step {
            Step::Wait => Outcome::Wait,
            Step::Land { train } => Outcome::Land {
                head: find(trains, &train)?.manifest.train_sha.clone(),
                train,
            },
            Step::Remerge { train } => {
                let t = find(trains, &train)?;
                let next = remerge_verified(
                    ctx.repo,
                    &t.manifest,
                    ctx.main_tip,
                    &t.parts,
                    &t.adopted,
                    ctx.protected,
                )?;
                let out = ctx.out.join(format!("{}.manifest.json", next.build));
                publish_swap(
                    ctx.repo,
                    &format!("land/train-{}", next.build),
                    &t.manifest,
                    &next,
                    &out,
                )?;
                Outcome::Remerged {
                    train,
                    head: next.train_sha,
                }
            }
            Step::Combine { trains: names } => {
                let found: Vec<&DriveTrain> = names
                    .iter()
                    .map(|n| find(trains, n))
                    .collect::<Result<_, _>>()?;
                let manifests: Vec<Manifest> = found.iter().map(|t| t.manifest.clone()).collect();
                let attributes = &manifests[0].policy.attributes;
                if manifests.iter().any(|m| &m.policy.attributes != attributes) {
                    return Err("combine: the trains carry different merge attributes".into());
                }
                let adopted: Vec<Adopted> = found.iter().flat_map(|t| t.adopted.clone()).collect();
                let build = format!("combined-{}", names.join("-"));
                let head = rebuild(
                    ctx,
                    &build,
                    super::combine::combine_parts(&manifests)?,
                    attributes,
                    &adopted,
                )?;
                Outcome::Combined {
                    trains: names,
                    build,
                    head,
                }
            }
            Step::DropPart { train, hints, log } => {
                let t = find(trains, &train)?;
                let (mut session, first) = Session::new(t.parts.clone());
                let actions = session.red(&first.id, &hints, &log);
                let evicted = actions
                    .iter()
                    .filter_map(|a| match a {
                        Action::Evict { pr, .. } => Some(*pr),
                        _ => None,
                    })
                    .collect();
                let next = actions.iter().find_map(|a| match a {
                    Action::Freeze(c) => Some(c.parts.clone()),
                    _ => None,
                });
                let rebuilt = match next {
                    Some(parts) => {
                        let name = format!("{train}-drop");
                        let head =
                            rebuild(ctx, &name, parts, &t.manifest.policy.attributes, &t.adopted)?;
                        Some((name, head))
                    }
                    None => None,
                };
                Outcome::Dropped {
                    train,
                    evicted,
                    rebuilt,
                }
            }
        });
    }
    Ok(outcomes)
}
