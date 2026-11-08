Fix hosted PR evidence lookup on older GitHub CLI versions by using `gh api
--paginate` without `--slurp` and decoding concatenated page arrays. Validate
every page before matching; reject empty output, malformed pages, and trailing
garbage without weakening merge provenance checks.
