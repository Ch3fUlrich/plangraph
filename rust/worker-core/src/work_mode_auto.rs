//! Pure work-mode selection from the planner's served writer snapshot.
use crate::work_mode::WorkMode;
use serde::{Deserialize, Serialize};

/// AA Intelligence 50 is the strong-writer floor (D-5952 / rule 3.41).
pub const STRONG_WRITER_THRESHOLD: f64 = 50.0;

#[derive(Debug, Deserialize)]
pub struct WriterLeg {
    pub id: String,
    pub aa_intelligence: Option<f64>,
    pub served: bool,
    pub unavailable_reason: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct Resolution {
    pub work_mode: WorkMode,
    pub work_mode_reason: String,
}

/// Manual settings do not inspect the snapshot. Only admitted, non-exhausted
/// writer legs may select batch; registration alone is insufficient.
pub fn resolve(requested: WorkMode, legs: &[WriterLeg]) -> Resolution {
    let (work_mode, work_mode_reason) = match requested {
        WorkMode::Batch => (WorkMode::Batch, "manual batch".into()),
        WorkMode::Piecewise => (WorkMode::Piecewise, "manual piecewise".into()),
        WorkMode::Auto => {
            let strong = |leg: &&WriterLeg| {
                leg.aa_intelligence
                    .is_some_and(|n| n.is_finite() && n >= STRONG_WRITER_THRESHOLD)
            };
            if let Some(leg) = legs
                .iter()
                .filter(strong)
                .find(|l| l.served && l.unavailable_reason.is_none())
            {
                (
                    WorkMode::Batch,
                    format!("auto: strong writer {} served", leg.id),
                )
            } else if let Some(leg) = legs.iter().find(strong) {
                (
                    WorkMode::Piecewise,
                    format!(
                        "auto: strong writer {} unavailable: {}; no strong writer served",
                        leg.id,
                        leg.unavailable_reason.as_deref().unwrap_or("not served")
                    ),
                )
            } else if let Some(leg) = legs.iter().find(|l| l.served) {
                (WorkMode::Piecewise, format!("auto: writer {} below strong-writer threshold or unscored; no strong writer served", leg.id))
            } else {
                (WorkMode::Piecewise, "auto: no writer legs served".into())
            }
        }
    };
    Resolution {
        work_mode,
        work_mode_reason,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn auto_served_writer_table() {
        for (score, served, unavailable, expected, reason) in [
            (
                Some(50.0),
                true,
                None,
                WorkMode::Batch,
                "strong writer deciding served",
            ),
            (
                Some(60.0),
                false,
                Some("quota_exhausted"),
                WorkMode::Piecewise,
                "deciding unavailable: quota_exhausted",
            ),
            (
                Some(60.0),
                true,
                Some("quota_exhausted"),
                WorkMode::Piecewise,
                "deciding unavailable: quota_exhausted",
            ),
            (
                Some(49.9),
                true,
                None,
                WorkMode::Piecewise,
                "writer deciding below",
            ),
            (
                None,
                true,
                None,
                WorkMode::Piecewise,
                "writer deciding below",
            ),
            (
                Some(f64::NAN),
                true,
                None,
                WorkMode::Piecewise,
                "writer deciding below",
            ),
        ] {
            let legs = [WriterLeg {
                id: "deciding".into(),
                aa_intelligence: score,
                served,
                unavailable_reason: unavailable.map(str::to_owned),
            }];
            let result = resolve(WorkMode::Auto, &legs);
            assert_eq!(result.work_mode, expected);
            assert!(
                result.work_mode_reason.contains(reason),
                "{}",
                result.work_mode_reason
            );
            for mode in [WorkMode::Batch, WorkMode::Piecewise] {
                assert_eq!(resolve(mode, &legs).work_mode, mode);
            }
        }
        assert_eq!(resolve(WorkMode::Auto, &[]).work_mode, WorkMode::Piecewise);
    }

    #[test]
    fn exhausted_first_strong_writer_does_not_hide_served_fallback() {
        let legs = [
            WriterLeg {
                id: "exhausted".into(),
                aa_intelligence: Some(60.0),
                served: false,
                unavailable_reason: Some("limited".into()),
            },
            WriterLeg {
                id: "fallback".into(),
                aa_intelligence: Some(50.0),
                served: true,
                unavailable_reason: None,
            },
        ];
        let result = resolve(WorkMode::Auto, &legs);
        assert_eq!(result.work_mode, WorkMode::Batch);
        assert!(result.work_mode_reason.contains("fallback"));
    }
}
