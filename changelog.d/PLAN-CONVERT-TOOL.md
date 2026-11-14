Added offline plan conversion through `python3 -m plangraph.keeper.cli plan convert`.
Rust generates one ordered atomic batch and hashes the actual input files and
operations. The existing keeper validators and Store dry-run reject invalid plans
before both output files are published. Includes generic CLI guard and boundary
tests plus an opt-in private historical fixture check with an explicitly labelled
reconstructed-baseline limitation. See docs/convert-plans.md.
