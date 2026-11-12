Local preflight now reports missing private scrub lists explicitly, bounds check
execution, prints summaries immediately, and never treats policy output as a
network failure. Push hooks announce bypasses and check each pushed ref range;
affected tests and revision-aware policy checks receive the selected head.

CI fix round: missing private lists remain non-blocking, installed hooks resolve
preflight from the checkout root, timeouts refuse with a labelled diagnostic,
and branch deletions skip content checks. Revision file-size scans batch binary
blobs before decoding. HTTP 5xx failures retry once and retain INFRA status;
preflight defaults to its script checkout and caps affected-test workers at two.
