use serde_json::Value;

const TESTS_METADATA_JOB: &str = "ci-pr-description";

/// A title alone is insufficient: old workflow configurations used the editable PR title.
fn verified_metadata_run(
    id: u64,
    attempt: u64,
    event: &Value,
    display_title: &Value,
    fetch_jobs: impl FnOnce(&str) -> Result<Value, String>,
) -> Result<bool, String> {
    if event != "pull_request" || display_title != format!("PR metadata check {id}").as_str() {
        return Ok(false);
    }
    let endpoint = format!("actions/runs/{id}/attempts/{attempt}/jobs?per_page=100");
    let response = fetch_jobs(&endpoint)?;
    let jobs = response["jobs"].as_array().ok_or("missing job list")?;
    if response["total_count"].as_u64() != Some(jobs.len() as u64) {
        return Err("incomplete job list for metadata run".into());
    }
    let alternate_count = jobs
        .iter()
        .filter(|job| job["name"] == TESTS_METADATA_JOB)
        .count();
    let metadata = alternate_count == 1
        && jobs.iter().all(|job| match job["name"].as_str() {
            Some(TESTS_METADATA_JOB) => true,
            Some(name) if !name.is_empty() && name != "ci-gate" => job["conclusion"] == "skipped",
            _ => false,
        });
    if metadata {
        return Ok(true);
    }
    if jobs
        .iter()
        .any(|job| job["name"] == "ci-gate" && job["conclusion"] != "skipped")
    {
        return Ok(false);
    }
    Err(format!(
        "run {id} attempt {attempt}: metadata marker's job is unconfirmed"
    ))
}

const DRAFT_PREFIX: &str = "Draft lane check ";
pub(crate) const DRAFT_WITNESS: &str = "Draft lane without test proof";
const SELECT_SUITE: &str = "Select suites for this change";

/// Ignore an off-path Tests run only after checking the exact attempt from Actions.
pub(crate) fn verified_non_test_run(
    id: u64,
    attempt: Option<u64>,
    event: &Value,
    title: &Value,
    fetch_jobs: impl FnOnce(&str) -> Result<Value, String>,
) -> Result<bool, String> {
    let Some(title) = title
        .as_str()
        .filter(|title| title.starts_with(DRAFT_PREFIX))
    else {
        return verified_metadata_run(id, attempt.unwrap_or(1), event, title, fetch_jobs);
    };
    let unconfirmed = || format!("run {id}: draft lane marker's job is unconfirmed");
    let attempt = attempt
        .filter(|attempt| *attempt > 0)
        .ok_or_else(unconfirmed)?;
    if event != "pull_request" || title != format!("{DRAFT_PREFIX}{id}") || id == 0 {
        return Err(unconfirmed());
    }
    let endpoint = format!("actions/runs/{id}/attempts/{attempt}/jobs?per_page=100");
    let response = fetch_jobs(&endpoint)?;
    let jobs = response["jobs"].as_array().ok_or_else(unconfirmed)?;
    if response["total_count"].as_u64() != Some(jobs.len() as u64) || !draft_jobs(jobs) {
        return Err(unconfirmed());
    }
    Ok(true)
}

fn skipped(row: &Value) -> bool {
    row["status"] == "completed" && row["conclusion"] == "skipped"
}

fn green(row: &Value) -> bool {
    row["status"] == "completed" && row["conclusion"] == "success"
}

fn draft_jobs(jobs: &[Value]) -> bool {
    let mut gate = jobs.iter().filter(|job| job["name"] == "ci-gate");
    let Some(job) = gate.next() else { return false };
    let Some(steps) = job["steps"].as_array() else {
        return false;
    };
    gate.next().is_none()
        && green(job)
        && jobs
            .iter()
            .any(|job| job["name"] == SELECT_SUITE && skipped(job))
        && jobs
            .iter()
            .all(|job| job["name"] == "ci-gate" || skipped(job))
        && steps
            .iter()
            .filter(|step| step["name"] == DRAFT_WITNESS)
            .count()
            == 1
        && steps.iter().all(|step| match step["name"].as_str() {
            Some(DRAFT_WITNESS | "Set up job" | "Complete job") => green(step),
            _ => skipped(step),
        })
}
