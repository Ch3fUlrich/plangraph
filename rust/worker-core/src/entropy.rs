#[cfg(test)]
#[path = "entropy_tests.rs"]
mod tests;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Baseline {
    pub test_source_ratio: f64,
    pub class_growth_fraction: f64,
    // Retained for baseline compatibility and reporting only; G1 is void (D-5881).
    pub max_changed_lines: u64,
    pub max_test_file_lines: u64,
    pub max_test_addition_ratio: f64,
}
impl Default for Baseline {
    fn default() -> Self {
        Self {
            test_source_ratio: 0.2,
            class_growth_fraction: 0.03,
            max_changed_lines: 400,
            max_test_file_lines: 800,
            max_test_addition_ratio: 1.0,
        }
    }
}
#[derive(Clone, Debug, Default)]
pub struct Inventory {
    pub classes: BTreeMap<String, u64>,
    pub source: u64,
    pub tests: u64,
    pub largest_test: u64,
}
impl Inventory {
    pub fn add(&mut self, path: &str, bytes: &[u8]) {
        if bytes.contains(&0) {
            return;
        }
        let lines = bytes.iter().filter(|b| **b == b'\n').count() as u64
            + u64::from(!bytes.is_empty() && bytes.last() != Some(&b'\n'));
        let class = if is_test(path) {
            self.tests += lines;
            self.largest_test = self.largest_test.max(lines);
            "test"
        } else if path.starts_with("src/") || path.starts_with("rust/") {
            self.source += lines;
            "source"
        } else if plan_report(path) {
            "plan-report"
        } else {
            "other"
        };
        *self.classes.entry(class.into()).or_default() += lines;
    }
    pub fn ratio(&self) -> f64 {
        if self.source == 0 {
            if self.tests == 0 {
                0.0
            } else {
                f64::INFINITY
            }
        } else {
            self.tests as f64 / self.source as f64
        }
    }
}
#[derive(Clone, Debug)]
pub struct Change {
    pub path: String,
    pub added: u64,
    pub deleted: u64,
    pub new: bool,
}
pub struct Input {
    pub changes: Vec<Change>,
    pub current: Inventory,
    pub week: Inventory,
    pub baseline: Baseline,
    pub prior_baseline: Option<Baseline>,
    pub body: String,
}
#[derive(Debug, Serialize)]
pub struct Finding {
    pub code: &'static str,
    pub fix: String,
}
pub fn is_test(path: &str) -> bool {
    let path = path.to_ascii_lowercase();
    path.split('/').any(|c| c == "tests" || c == "test")
        || path.rsplit('/').next().is_some_and(|s| {
            s.starts_with("test_")
                || s.ends_with("_tests.rs")
                || s.ends_with(".test.ts")
                || s.ends_with(".test.js")
        })
}
fn plan_report(path: &str) -> bool {
    let path = path.to_ascii_lowercase();
    !path.starts_with("changelog.d/")
        && path.ends_with(".md")
        && (path
            .split('/')
            .any(|s| ["plan", "plans", "report", "reports"].contains(&s))
            || path
                .rsplit('/')
                .next()
                .is_some_and(|s| s.starts_with("plan") || s.starts_with("report")))
}
fn reason(body: &str, name: &str) -> bool {
    body.lines()
        .any(|s| s.strip_prefix(name).is_some_and(|s| !s.trim().is_empty()))
}
fn valid(b: &Baseline) -> bool {
    b.test_source_ratio.is_finite()
        && b.test_source_ratio >= 0.0
        && b.class_growth_fraction.is_finite()
        && (0.0..=0.03).contains(&b.class_growth_fraction)
        && b.max_test_file_lines <= 800
        && b.max_test_addition_ratio.is_finite()
        && (0.0..=1.0).contains(&b.max_test_addition_ratio)
}
pub fn check(p: &Input) -> Vec<Finding> {
    let mut failures = Vec::new();
    let mut add = |code, fix: String| failures.push(Finding { code, fix });
    let b = &p.baseline;
    let raised = p.prior_baseline.as_ref().is_some_and(|old| {
        b.test_source_ratio > old.test_source_ratio
            || b.class_growth_fraction > old.class_growth_fraction
            // D-5881 explicitly migrates the former 400-line limit to 800.
            || (b.max_test_file_lines > old.max_test_file_lines
                && !(old.max_test_file_lines == 400 && b.max_test_file_lines == 800))
            || b.max_test_addition_ratio > old.max_test_addition_ratio
    });
    if !valid(b) || raised {
        add(
            "baseline",
            "restore or lower baseline thresholds; increases are forbidden".into(),
        );
    }
    for (class, now) in &p.current.classes {
        let old = *p.week.classes.get(class).unwrap_or(&0);
        if *now as f64 > old as f64 * (1.0 + b.class_growth_fraction) {
            add(
                "G2",
                format!("reduce {class} weekly net growth to at most 3% ({old} -> {now})"),
            );
        }
    }
    let (test_added, non_test_added) = p.changes.iter().fold((0_u64, 0_u64), |(t, n), c| {
        if is_test(&c.path) {
            (t.saturating_add(c.added), n)
        } else {
            (t, n.saturating_add(c.added))
        }
    });
    if test_added as f64 > non_test_added as f64 * b.max_test_addition_ratio
        && !reason(&p.body, "Test-Heavy:")
    {
        add(
            "G3-added",
            "reduce test additions or supply Test-Heavy: with a reason".into(),
        );
    }
    if p.current.ratio() > b.test_source_ratio {
        add(
            "G3-ratio",
            "reduce tests/(src+rust) to the stored baseline; the baseline may only shrink".into(),
        );
    }
    if p.current.largest_test > b.max_test_file_lines {
        add(
            "G3-file",
            format!(
                "split each test file into files of at most {} lines",
                b.max_test_file_lines
            ),
        );
    }
    for c in p.changes.iter().filter(|c| c.new && plan_report(&c.path)) {
        add(
            "G4",
            format!(
                "update an existing plan/report instead of adding {}",
                c.path
            ),
        );
    }
    failures
}
