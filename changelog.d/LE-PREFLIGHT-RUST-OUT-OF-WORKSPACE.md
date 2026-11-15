Pre-push Rust checks select excluded packages by their own Cargo manifest even
when they do not declare a workspace table. Regression coverage verifies that
worker-only changes run manifest-based format, lint and offline worker tests.
