//! The driver's decisions for several landing trains in CI at once (D-6774, rule 3.63 as amended): the
//! first green train lands, every other train that is green or still running meets the new main, a red
//! train drops its attributable part. Pure: the caller reads the trains and the live tip of main and runs
//! the steps (`land-train plan` prints them), then asks again after each step.
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Ci {
    Running,
    Green,
    /// Red: the part pull requests the failing test paths name (may be empty) and the job log.
    Red {
        hints: Vec<u64>,
        log: String,
    },
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct TrainState {
    pub name: String,
    /// The tip of main the tested head contains: the builder's base, or the last remerged main.
    pub main_in: String,
    pub ci: Ci,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Step {
    /// Land this train: it is green and contains the live tip of main.
    Land { train: String },
    /// Merge the new main into this train (`land-train remerge`); green needs one more full CI and the
    /// remerge-diff review, never a part review or a fix round.
    Remerge { train: String },
    /// Several stale trains: build ONE train of the union of their parts on the new main
    /// (`land-train combine`), so the full CI runs once.
    Combine { trains: Vec<String> },
    /// A red train: leave out the attributable part and continue (`split::Session::red` with these hints).
    DropPart {
        train: String,
        hints: Vec<u64>,
        log: String,
    },
    /// Nothing to do until a run finishes.
    Wait,
}

/// The next steps for `trains` given in the order their CI finished. A landing comes first and alone
/// (main moves with it); without one, stale trains meet main and red trains drop a part.
pub fn plan(main_tip: &str, trains: &[TrainState]) -> Vec<Step> {
    // A landing comes first and alone: recovery work (drops, rebuilds) waits until main is read again, so
    // a failing rebuild can never hide a green train's landing.
    if let Some(first) = trains
        .iter()
        .find(|t| t.ci == Ci::Green && t.main_in == main_tip)
    {
        return vec![Step::Land {
            train: first.name.clone(),
        }];
    }
    let mut steps: Vec<Step> = trains
        .iter()
        .filter_map(|t| match &t.ci {
            Ci::Red { hints, log } => Some(Step::DropPart {
                train: t.name.clone(),
                hints: hints.clone(),
                log: log.clone(),
            }),
            _ => None,
        })
        .collect();
    let stale: Vec<String> = trains
        .iter()
        .filter(|t| !matches!(t.ci, Ci::Red { .. }))
        .filter(|t| t.main_in != main_tip)
        .map(|t| t.name.clone())
        .collect();
    match stale.len() {
        0 => {}
        1 => steps.push(Step::Remerge {
            train: stale[0].clone(),
        }),
        _ => steps.push(Step::Combine { trains: stale }),
    }
    if steps.is_empty() {
        steps.push(Step::Wait);
    }
    steps
}
