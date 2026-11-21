use super::{write_observations, Observation, Result};
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::Value;

pub(super) struct Model {
    id: String,
    name: String,
    canonical_slug: Option<String>,
    index_version: Option<String>,
    score: Option<f64>,
    prompt_price: Option<f64>,
    completion_price: Option<f64>,
}

fn text(value: &Value) -> Option<String> {
    value
        .as_str()
        .filter(|s| !s.trim().is_empty())
        .map(str::to_owned)
}

fn number(value: &Value) -> Option<f64> {
    value
        .as_f64()
        .or_else(|| value.as_str()?.parse().ok())
        .filter(|n| n.is_finite() && *n >= 0.0)
}

pub(super) fn models(body: &[u8]) -> Result<Vec<Model>> {
    let data: Value = serde_json::from_slice(body)?;
    let rows = data["data"]
        .as_array()
        .ok_or("OpenRouter response has no model list")?;
    let mut models = Vec::with_capacity(rows.len());
    let mut seen = std::collections::HashSet::new();
    for row in rows {
        let id = text(&row["id"]).ok_or("OpenRouter model has no id")?;
        if !seen.insert(id.clone()) {
            return Err("OpenRouter model id is duplicated".into());
        }
        let aa = &row["benchmarks"]["artificial_analysis"];
        models.push(Model {
            name: text(&row["name"]).unwrap_or_else(|| id.clone()),
            id,
            canonical_slug: text(&row["canonical_slug"]),
            index_version: text(&aa["index_version"])
                .or_else(|| aa["index_version"].as_number().map(ToString::to_string)),
            score: number(&aa["intelligence_index"]).filter(|n| *n <= 100.0),
            prompt_price: number(&row["pricing"]["prompt"]),
            completion_price: number(&row["pricing"]["completion"]),
        });
    }
    Ok(models)
}

fn record_flags(db: &Connection, model: &Model, reason: Option<&str>) -> Result<()> {
    db.execute(
        "INSERT INTO catalog_benchmark_refresh_flags
            (model_id, effort, harness, stale_reason, repriced_cost, price_disagreement)
         SELECT DISTINCT cells.model_id, cells.effort, cells.harness,
            CASE WHEN ?2 != 'price_changed' OR prices.openrouter_id IS NOT NULL THEN ?2 END,
            prices.prompt_tokens * ?3 + prices.completion_tokens * ?4,
            COALESCE(abs(prices.reference_prompt_price - ?3) > prices.reference_prompt_price * 0.05, 0)
                OR COALESCE(abs(prices.reference_completion_price - ?4) > prices.reference_completion_price * 0.05, 0)
         FROM catalog_benchmark_rows cells LEFT JOIN catalog_benchmark_price_inputs prices
            USING (model_id, effort, harness)
         WHERE (cells.model_id=?1 OR prices.openrouter_id=?1)
            AND (?2 IS NOT NULL OR prices.openrouter_id IS NOT NULL)
         ON CONFLICT (model_id, effort, harness) DO UPDATE SET
            stale_reason=COALESCE(excluded.stale_reason,catalog_benchmark_refresh_flags.stale_reason),
            repriced_cost=excluded.repriced_cost, price_disagreement=excluded.price_disagreement",
        params![model.id, reason, model.prompt_price, model.completion_price],
    )?;
    Ok(())
}

pub(super) fn apply_models(
    db: &Connection,
    models: Vec<Model>,
    url: &str,
    date: &str,
) -> Result<usize> {
    let previous: bool = db.query_row(
        "SELECT EXISTS(SELECT 1 FROM catalog_benchmark_openrouter_models)",
        [],
        |r| r.get(0),
    )?;
    let mut prior = db.prepare_cached(
        "SELECT canonical_slug,index_version,prompt_price,completion_price
        FROM catalog_benchmark_openrouter_models WHERE id=?1",
    )?;
    let mut save = db.prepare_cached(
        "INSERT OR REPLACE INTO catalog_benchmark_openrouter_models VALUES (?1,?2,?3,?4,?5)",
    )?;
    let mut written = 0;
    for model in models {
        let old = prior
            .query_row([&model.id], |row| {
                Ok((
                    row.get::<_, Option<String>>(0)?,
                    row.get::<_, Option<String>>(1)?,
                    row.get::<_, Option<f64>>(2)?,
                    row.get::<_, Option<f64>>(3)?,
                ))
            })
            .optional()?;
        let reason = match old {
            Some((slug, _, _, _))
                if slug != model.canonical_slug && model.canonical_slug.is_some() =>
            {
                Some("served_model_changed")
            }
            Some((_, index, _, _))
                if index != model.index_version && model.index_version.is_some() =>
            {
                Some("index_version_changed")
            }
            Some((_, _, prompt, completion))
                if prompt != model.prompt_price || completion != model.completion_price =>
            {
                Some("price_changed")
            }
            None if previous => Some("new_model_id"),
            _ => None,
        };
        save.execute(params![
            model.id,
            model.canonical_slug,
            model.index_version,
            model.prompt_price,
            model.completion_price
        ])?;
        if model.score.is_some() {
            // The listing reports supported invocation efforts, not the effort the index used.
            written += write_observations(
                db,
                vec![Observation {
                    model: model.id.clone(),
                    effort: String::new(),
                    harness: "openrouter".into(),
                    origin: "aa-index",
                    score: model.score,
                    score_se: None,
                    runs: None,
                    steps: None,
                    cost: None,
                    tokens: None,
                    source_url: url.into(),
                    source_record: model.name.clone(),
                    index_version: model.index_version.clone(),
                    measured_at: None,
                }],
                date,
            )?;
        }
        record_flags(db, &model, reason)?;
    }
    Ok(written)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> Connection {
        let db = Connection::open_in_memory().unwrap();
        db.execute_batch(super::super::SCHEMA).unwrap();
        db.execute_batch(
            "INSERT INTO catalog_benchmark_measurements
                (model_id,effort,harness,origin,score,cost_per_task_usd,measured_at)
                VALUES ('leg/model','high','codex','own',50,4,datetime('now'));
             INSERT INTO catalog_benchmark_price_inputs VALUES
                ('leg/model','high','codex','provider/model',1000,100,0.001,0.01);",
        )
        .unwrap();
        db
    }

    fn model(id: &str, price: f64, version: &str) -> Model {
        Model {
            id: id.into(),
            name: id.into(),
            canonical_slug: None,
            index_version: Some(version.into()),
            score: Some(52.0),
            prompt_price: Some(price),
            completion_price: Some(0.01),
        }
    }

    fn apply(db: &Connection, model: Model) {
        apply_models(
            db,
            vec![model],
            "https://example.invalid/models",
            "2026-11-21",
        )
        .unwrap();
    }

    fn flag(db: &Connection, id: &str) -> (bool, String, Option<f64>, bool) {
        db.query_row(
            "SELECT stale,stale_reason,cost_per_task_usd,price_disagreement
            FROM catalog_benchmark_rows WHERE model_id=?1",
            [id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .unwrap()
    }

    #[test]
    fn an_allowlisted_price_change_reprices_and_flags_disagreement() {
        let db = fixture();
        apply(&db, model("provider/model", 0.001, "5.0"));
        apply(&db, model("provider/model", 0.002, "5.0"));
        assert_eq!(
            flag(&db, "leg/model"),
            (true, "price_changed".into(), Some(3.0), true)
        );
    }

    #[test]
    fn a_new_openrouter_identity_is_marked_stale() {
        let db = fixture();
        apply(&db, model("provider/model", 0.001, "5.0"));
        apply(&db, model("provider/new-model", 0.001, "5.0"));
        assert_eq!(
            flag(&db, "provider/new-model"),
            (true, "new_model_id".into(), None, false)
        );
    }

    #[test]
    fn a_new_index_version_flags_the_observation() {
        let db = fixture();
        apply(&db, model("provider/model", 0.001, "5.0"));
        apply(&db, model("provider/model", 0.001, "5.1"));
        assert_eq!(
            flag(&db, "provider/model"),
            (true, "index_version_changed".into(), None, false)
        );
    }
}
