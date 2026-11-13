# Python parity corpus

The source implementations and tests were read from the data repository's
origin/main at `ece6e4bf200f0988646f92c727f4c5cf5ddb95cc` before each port.
The source checkout was never edited. Corpus inputs come from its synthetic
router tests, not from live sessions, keeper state or credentials.

| Corpus | Source tests | Recorded cases |
| --- | --- | ---: |
| handoff.json | test_handoff_check.py | 50 |
| digest.jsonl | test_resume_digest.py | 123 executions |
| go.jsonl | test_go_row_gate.py, test_go_row_gate_grammar.py | 220 executions |
| backfill_parse.jsonl | test_inreview_backfill.py, test_inreview_backfill_binding.py | 146 unique rows |
| backfill_report.jsonl | the same backfill tests | 147 report/missing-report pairs |
| summary.jsonl | test_work_summary.py | 33 executions |

Each execution records file contents and subprocess replies before comparing
the complete output. Temporary roots and transcript slugs become placeholders;
replay reconstructs them with equal-length native paths so truncation remains
observable. Captured local git replies are complemented by native temporary
repository integration tests. Fake project labels replace deployment-specific
labels. Native helper tests carry project-registry schema and session mapping
cases without copying the private registry or deployment skill prose.

The fake token strings, placeholder path fragments and temporary commit IDs
flagged by the public scrubber have exact file/rule/value-hash allowances in
the public split manifest. They remain visible for review; no entire fixture
or rule is excluded. Gitleaks also scans the crate.

The principal/lease incident tests intentionally change legacy LAPSED behavior.
The historical incident snapshot was not supplied; those regressions use its
reported keys and principal shape. The reference digest suite had one
path-length-sensitive assertion failure in its dense-page scenario, whose
complete output is still included. Other recorded reference suites passed.
