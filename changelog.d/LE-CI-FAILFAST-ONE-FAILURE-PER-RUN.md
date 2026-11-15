Disable fail-fast for both pytest CI matrices so sibling shards report every
failure in one run; any failed shard still fails ci-gate. Local affected tests
always include subprocess UTF-8, file-size, language, and changelog/docs policy
tests, subject to explicit suite restrictions.
