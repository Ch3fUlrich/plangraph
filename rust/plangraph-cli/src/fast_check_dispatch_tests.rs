use super::*;
use std::collections::BTreeMap;

fn host(name: &str, floor: u64) -> Candidate {
    Candidate {
        name: name.into(),
        route: None,
        wsl_distro: None,
        cwd: ".".into(),
        mem_floor_mb: floor,
    }
}
fn sample(mem: u64, cpus: usize, load1: Option<f64>) -> Sample {
    Sample {
        mem_available_mb: mem,
        cpus,
        load1,
    }
}
const BUSY: &str = r#"{"reason": "host_busy", "load_per_cpu": 0.93, "cpus": 8}"#;

/// Samples by host name; `runs` pops the next outcome per host.
struct Fixture {
    samples: BTreeMap<String, Sample>,
    runs: BTreeMap<String, Vec<Outcome>>,
    ran: Vec<String>,
}
impl Fixture {
    fn new(samples: &[(&str, Sample)], runs: &[(&str, Vec<Outcome>)]) -> Self {
        Fixture {
            samples: samples
                .iter()
                .map(|(n, s)| (n.to_string(), s.clone()))
                .collect(),
            runs: runs
                .iter()
                .map(|(n, o)| (n.to_string(), o.clone()))
                .collect(),
            ran: vec![],
        }
    }
}
impl Fleet for Fixture {
    fn sample(&mut self, host: &Candidate) -> Option<Sample> {
        self.samples.get(&host.name).cloned()
    }
    fn run(&mut self, host: &Candidate) -> Outcome {
        self.ran.push(host.name.clone());
        self.runs.get_mut(&host.name).unwrap().remove(0)
    }
}
fn out(code: i32, text: &str) -> Outcome {
    Outcome {
        code,
        output: text.into(),
    }
}

#[test]
fn load_is_normalized_by_cpus_and_unknown_load_is_never_eligible() {
    let h = host("a", 100);
    assert_eq!(load_per_cpu(&sample(1, 8, Some(4.0))), Some(0.5));
    assert_eq!(load_per_cpu(&sample(1, 0, Some(4.0))), None);
    assert_eq!(load_per_cpu(&sample(1, 8, None)), None);
    assert_eq!(load_per_cpu(&sample(1, 8, Some(f64::NAN))), None);
    assert!(!eligible(&h, &sample(1000, 8, None)));
}

#[test]
fn threshold_rejects_load_at_or_above_point_seven_per_cpu() {
    let h = host("a", 100);
    assert!(eligible(&h, &sample(1000, 10, Some(6.9))));
    assert!(!eligible(&h, &sample(1000, 10, Some(7.0))));
    assert!(!eligible(&h, &sample(1000, 10, Some(9.0))));
}

#[test]
fn memory_floor_is_inclusive() {
    let h = host("a", 4096);
    assert!(!eligible(&h, &sample(4095, 8, Some(0.0))));
    assert!(eligible(&h, &sample(4096, 8, Some(0.0))));
}

#[test]
fn placement_chooses_the_less_loaded_eligible_host() {
    let hosts = [host("busy", 1), host("calm", 1), host("full", 1_000_000)];
    let mut fleet = Fixture::new(
        &[
            ("busy", sample(10_000, 8, Some(5.0))),
            ("calm", sample(10_000, 8, Some(1.0))),
            ("full", sample(10, 8, Some(0.0))),
        ],
        &[("calm", vec![out(0, "ok")])],
    );
    let done = dispatch(&mut fleet, &hosts).unwrap();
    assert_eq!(fleet.ran, ["calm"]);
    assert_eq!(done.verdict, Verdict::Pass);
}

#[test]
fn no_eligible_host_names_every_rejection() {
    let hosts = [host("a", 1), host("b", 1), host("c", 1)];
    let mut fleet = Fixture::new(
        &[
            ("a", sample(10_000, 4, Some(3.0))),
            ("b", sample(10_000, 4, None)),
        ],
        &[],
    );
    let error = dispatch(&mut fleet, &hosts).unwrap_err();
    for needle in ["a: load 0.75 per cpu", "b: load unknown", "c: no probe"] {
        assert!(error.contains(needle), "{needle}: {error}");
    }
    assert!(fleet.ran.is_empty());
}

#[test]
fn host_busy_retries_once_on_another_eligible_host() {
    let hosts = [host("a", 1), host("b", 1)];
    let mut fleet = Fixture::new(
        &[
            ("a", sample(10_000, 8, Some(0.1))),
            ("b", sample(10_000, 8, Some(2.0))),
        ],
        &[("a", vec![out(1, BUSY)]), ("b", vec![out(0, "ok")])],
    );
    let done = dispatch(&mut fleet, &hosts).unwrap();
    assert_eq!(fleet.ran, ["a", "b"]);
    assert_eq!(done.verdict, Verdict::Pass);
    let verdicts: Vec<_> = done.attempts.iter().map(|a| a.verdict).collect();
    assert_eq!(verdicts, [Verdict::HostBusy, Verdict::Pass]);
}

#[test]
fn a_second_host_busy_is_terminal_and_the_third_host_is_not_tried() {
    let hosts = [host("a", 1), host("b", 1), host("c", 1)];
    let all = sample(10_000, 8, Some(0.1));
    let mut fleet = Fixture::new(
        &[("a", all.clone()), ("b", all.clone()), ("c", all)],
        &[("a", vec![out(1, BUSY)]), ("b", vec![out(1, BUSY)])],
    );
    let done = dispatch(&mut fleet, &hosts).unwrap();
    assert_eq!(fleet.ran, ["a", "b"]);
    assert_eq!(done.verdict, Verdict::HostBusy);
    assert_eq!(done.code, 1);
}

#[test]
fn busy_retry_needs_another_eligible_host() {
    let hosts = [host("a", 1), host("b", 1)];
    let mut fleet = Fixture::new(
        &[
            ("a", sample(10_000, 8, Some(0.1))),
            ("b", sample(10_000, 8, Some(7.0))),
        ],
        &[("a", vec![out(1, BUSY)])],
    );
    let done = dispatch(&mut fleet, &hosts).unwrap();
    assert_eq!(fleet.ran, ["a"]);
    assert_eq!(done.verdict, Verdict::HostBusy);
}

#[test]
fn a_real_test_failure_is_terminal_even_when_text_mentions_busy() {
    let hosts = [host("a", 1), host("b", 1)];
    let all = sample(10_000, 8, Some(0.1));
    let mut fleet = Fixture::new(
        &[("a", all.clone()), ("b", all)],
        &[(
            "a",
            vec![out(
                1,
                "FAILED tests/test_x.py::test_host_busy - assert 1 == 2\n",
            )],
        )],
    );
    let done = dispatch(&mut fleet, &hosts).unwrap();
    assert_eq!(fleet.ran, ["a"]);
    assert_eq!(done.verdict, Verdict::TestFailure);
    assert_eq!(done.code, 1);
}

#[test]
fn only_a_failing_run_can_be_busy() {
    assert_eq!(classify(&out(0, BUSY)), Verdict::Pass);
    assert_eq!(classify(&out(2, BUSY)), Verdict::HostBusy);
    assert_eq!(
        classify(&out(1, "noise\n{\"reason\": \"other\"}\n")),
        Verdict::TestFailure
    );
}

#[test]
fn the_report_counts_busy_retries_apart_from_test_failures() {
    let rows = [
        r#"{"host":"a","attempt":1,"verdict":"host_busy"}"#,
        r#"{"host":"b","attempt":2,"verdict":"pass"}"#,
        r#"{"host":"a","attempt":1,"verdict":"test_failure"}"#,
        "not json",
        r#"{"host":"a","attempt":1,"verdict":"pass"}"#,
    ]
    .join("\n");
    let counts = summarize(&rows);
    assert_eq!(
        (
            counts.passed,
            counts.host_busy,
            counts.test_failure,
            counts.ignored
        ),
        (2, 1, 1, 1)
    );
}
