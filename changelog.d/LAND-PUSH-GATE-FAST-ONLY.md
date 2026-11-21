The pre-push hook now has a lane path and a train path and runs no test on either: `preflight --lane`
runs scrub, commit policy, file size, new-code language, changelog, skill rule lint and cargo fmt;
a push to `land/train-*` is validated against its exact parent manifest instead of the open-pull-request
parent check. The affected tests run once, in CI.
