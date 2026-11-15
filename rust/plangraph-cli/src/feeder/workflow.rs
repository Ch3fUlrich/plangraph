use super::*;
use std::fmt;
#[derive(Debug)]
pub enum ClaimError {
    Refused(String),
    Unavailable(String),
}
impl fmt::Display for ClaimError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Refused(e) => write!(f, "claim refused: {e}"),
            Self::Unavailable(e) => write!(f, "claim unavailable: {e}"),
        }
    }
}
#[derive(Debug)]
pub enum SpawnError {
    Claim(ClaimError),
    Start(String),
}
impl fmt::Display for SpawnError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Claim(e) => e.fmt(f),
            Self::Start(e) => e.fmt(f),
        }
    }
}
/// One bounded tick. Refused claims are decisions about that item, not a reason
/// to starve the rest of the order. Infrastructure/start failures stop the tick.
#[allow(clippy::too_many_arguments)]
pub fn tick(
    ready: &Value,
    runs: &[Run],
    facts: &HostFacts,
    policy: &Policy,
    rules: &str,
    repo: &str,
    reviews: &str,
    dry: bool,
    launch: &mut impl Launch,
) -> Value {
    let (mut ready, mut skipped) = match prepare_ready(ready) {
        Ok(v) => v,
        Err(e) => return json!({"decision":"refused","reason":e,"error":true}),
    };
    while let Some(item) = pick(&ready, runs, policy) {
        if let Admission::Refuse(reason) = admit(facts, policy) {
            return json!({"decision":"refused","item_key":item.key,"reason":reason,"skipped":skipped});
        }
        let c = card(&item, repo, reviews, policy);
        match dispatch(dry, &c, &brief(&item, rules), launch) {
            Ok(mut row) => {
                row["item_key"] = json!(item.key);
                row["skipped"] = json!(skipped);
                return row;
            }
            Err(SpawnError::Claim(ClaimError::Refused(reason))) => {
                skipped.push(json!({"item_key":item.key,"reason":reason}));
                ready["items"]
                    .as_array_mut()
                    .expect("validated items")
                    .retain(|v| v["key"] != item.key);
            }
            Err(e) => {
                return json!({"decision":"refused","item_key":item.key,"reason":e.to_string(),"skipped":skipped,"error":true})
            }
        }
    }
    json!({"decision":"idle","reason":"no_eligible_item","skipped":skipped})
}
