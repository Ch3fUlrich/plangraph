#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn each_rule_has_below_boundary_above() {
        let mut s = Snapshot {
            stuck: Default::default(),
            pace: 65.0,
            budget: 66.0,
            ci_failed: 0,
            ci_total: 10,
            infra_failed: 0,
            review_age_hours: 23.0,
            review_rounds: 2,
            critical_idle_hours: 11.0,
            coding_vm_runs: 0,
            dell_idle: true,
            host_drift: false,
            closes_recent: 5,
            closes_previous: 4,
        };
        assert!(findings(&s).is_empty());
        s.pace = 66.0;
        s.ci_failed = 1;
        s.review_age_hours = 24.0;
        s.review_rounds = 3;
        s.critical_idle_hours = 12.0;
        s.closes_recent = 4;
        assert!(findings(&s).is_empty());
        s.pace = 242.0;
        s.ci_failed = 2;
        s.review_age_hours = 25.0;
        s.review_rounds = 4;
        s.critical_idle_hours = 13.0;
        s.coding_vm_runs = 1;
        s.host_drift = true;
        s.closes_recent = 3;
        assert_eq!(findings(&s).len(), 8);
        s.infra_failed = 2;
        assert!(!ci(&s));
        s.dell_idle = false;
        assert!(!placement(&s));
        s.ci_total = 0;
        s.ci_failed = 0;
        s.infra_failed = 0;
        assert!(!ci(&s));
    }
}
