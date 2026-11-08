Local preflight now reports missing private scrub lists explicitly, bounds check
execution, prints summaries immediately, and never treats policy output as a
network failure. Push hooks announce bypasses and check each pushed ref range;
affected tests and revision-aware policy checks receive the selected head.
