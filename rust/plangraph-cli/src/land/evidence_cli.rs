//! `plangraph land-train evidence-assess` and `evidence-report`: the carry-over decision for one batch
//! and the per-day counts of rebases, carries, re-reviews and rebase-caused fix rounds.
use super::evidence::{
    classify_red, daily, may_merge, records_of, voids_accept, Accept, Ci, CiRerun, Decision,
    Evidence, Kind, ReReview, Record, Red, RedKind, Void,
};
use super::evidence_git;
use super::main_state::State;
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
    if let Some(out) = flags.get("records-out") {
        append(out, &records_of(&d, need(flags, "day")?))?;
    }
    Ok(lines(&d))
}

fn append(path: &str, records: &[Record]) -> Result<(), String> {
    use std::io::Write;
    let mut f = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|e| format!("{path}: {e}"))?;
    for r in records {
        let line = serde_json::to_string(r).map_err(|e| e.to_string())?;
        writeln!(f, "{line}").map_err(|e| format!("{path}: {e}"))?;
    }
    Ok(())
}

/// `--records-out FILE --day D --kind fix-round-after-rebase`: a fix round that a rebase caused.
pub(super) fn record_cli(flags: &BTreeMap<String, String>) -> Result<Vec<String>, String> {
    let kind = match need(flags, "kind")?.as_str() {
        "fix-round-after-rebase" => Kind::FixRoundAfterRebase,
        other => {
            return Err(format!(
                "unknown --kind {other}: use fix-round-after-rebase"
            ))
        }
    };
    append(
        need(flags, "records-out")?,
        &[Record::new(need(flags, "day")?, kind)],
    )?;
    Ok(Vec::new())
}

/// `--red FILE --ci-green BOOL --main-state FILE`: whether a red voids a carried ACCEPT, and whether
/// the merge gate would let the batch in. The two answers never depend on each other.
pub(super) fn red_cli(flags: &BTreeMap<String, String>) -> Result<Vec<String>, String> {
    let read = |k: &str| -> Result<Vec<u8>, String> {
        let p = need(flags, k)?;
        std::fs::read(p).map_err(|e| format!("{p}: {e}"))
    };
    let red: Red = serde_json::from_slice(&read("red")?).map_err(|e| format!("--red: {e}"))?;
    let main: State =
        serde_json::from_slice(&read("main-state")?).map_err(|e| format!("--main-state: {e}"))?;
    let ci_green = need(flags, "ci-green")? == "true";
    let kind = classify_red(&red);
    let verdict = match (kind, voids_accept(kind)) {
        (RedKind::Flake, false) => "RED flake: a carried ACCEPT stays".to_owned(),
        _ => "RED genuine: a carried ACCEPT is void".to_owned(),
    };
    let merge = match may_merge(ci_green, &main) {
        Ok(()) => "MERGE ok".to_owned(),
        Err(word) => format!("MERGE refused {word}"),
    };
    Ok(vec![verdict, merge])
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
