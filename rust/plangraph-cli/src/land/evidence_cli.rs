//! `plangraph land-train evidence-assess` and `evidence-report`: the carry-over decision for one batch
//! and the per-day counts of rebases, carries, re-reviews and rebase-caused fix rounds.
use super::evidence::{daily, Accept, Ci, CiRerun, Decision, Evidence, ReReview, Record, Void};
use super::evidence_git;
use std::collections::BTreeMap;
use std::path::Path;

fn void_word(v: &Void) -> String {
    match v {
        Void::Conflict => "conflict".into(),
        Void::PatchIdChanged => "patch_id_changed".into(),
        Void::PathsChanged => "paths_changed".into(),
        Void::MainTouched(p) => format!(
            "main_touched {}",
            p.iter().cloned().collect::<Vec<_>>().join(",")
        ),
    }
}

/// The one-line-per-verdict text of a decision; every carry names the old and the new head.
pub fn lines(d: &Decision) -> Vec<String> {
    let accept = match &d.accept {
        Accept::Carried { old_head, new_head } => {
            format!("ACCEPT carried {old_head} -> {new_head}")
        }
        Accept::Void { reason, re_review } => format!(
            "ACCEPT void {} re-review {}",
            void_word(reason),
            match re_review {
                ReReview::Full => "full".to_owned(),
                ReReview::RangeDiff { old_head, new_head } =>
                    format!("range-diff {old_head}..{new_head}"),
            }
        ),
        Accept::None => "ACCEPT none".to_owned(),
    };
    let ci = match &d.ci {
        Ci::Carried { old_head, new_head } => format!("CI carried {old_head} -> {new_head}"),
        Ci::Rerun(CiRerun::Void(v)) => format!("CI rerun {}", void_word(v)),
        Ci::Rerun(CiRerun::TrainBound { landed, bound }) => {
            format!("CI rerun train_bound {landed} trains landed, bound {bound}")
        }
        Ci::Rerun(CiRerun::NoPassingCi) => "CI rerun no_passing_ci".to_owned(),
    };
    vec![accept, ci]
}

fn need<'a>(flags: &'a BTreeMap<String, String>, k: &str) -> Result<&'a String, String> {
    flags.get(k).ok_or_else(|| format!("missing --{k}"))
}

pub(super) fn assess_cli(flags: &BTreeMap<String, String>) -> Result<Vec<String>, String> {
    let path = need(flags, "evidence")?;
    let old: Evidence =
        serde_json::from_slice(&std::fs::read(path).map_err(|e| format!("{path}: {e}"))?)
            .map_err(|e| format!("{path}: {e}"))?;
    let trains: u64 = need(flags, "trains-now")?
        .parse()
        .map_err(|_| "--trains-now is not a number".to_owned())?;
    let d = evidence_git::assess(
        Path::new(need(flags, "repo")?),
        &old,
        need(flags, "main")?,
        need(flags, "head")?,
        trains,
    )?;
    Ok(lines(&d))
}

/// `--records FILE` holds one JSON record per line.
pub(super) fn report_cli(flags: &BTreeMap<String, String>) -> Result<Vec<String>, String> {
    let path = need(flags, "records")?;
    let text = std::fs::read_to_string(path).map_err(|e| format!("{path}: {e}"))?;
    let mut records = Vec::new();
    for (n, l) in text
        .lines()
        .enumerate()
        .filter(|(_, l)| !l.trim().is_empty())
    {
        records
            .push(serde_json::from_str::<Record>(l).map_err(|e| format!("{path}:{}: {e}", n + 1))?);
    }
    Ok(daily(&records)?
        .into_iter()
        .map(|(day, r)| {
            format!(
                "{day} rebases {} accepts_carried {} ci_carried {} re_reviews {} fix_rounds_after_rebase {}",
                r.rebases, r.accepts_carried, r.ci_carried, r.re_reviews, r.fix_rounds_after_rebase
            )
        })
        .collect())
}
