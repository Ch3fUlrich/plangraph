Store benchmark observations by model, effort, harness and origin. Catalog queries and utility use the preferred per-cell view with provenance, imputation and staleness. Existing Artificial Analysis rows remain readable as priors. Preserve explicit zeros and SQL NULLs in storage and catalog exports.
Legs with unspecified effort can use the legacy catalog effort for their exact model and a compatible harness.
Keep legacy benchmark views readable during registry column migration and preserve the route JSON label for catalog-backed utility.
The benchmark catalog integration test checks that strict routing refuses an absent effort cell.
DONE check for this batch under rule 9.14: `cargo test --manifest-path rust/Cargo.toml -p pg-spawnd --test benchmark_rows`.
