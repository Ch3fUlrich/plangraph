
## 2026-10-15 ~18:35-18:40Z (build-L1 gen-3), operator verbatim
- "Why do you wait for a qwen Seat? Should there be enough parallel runs possible?"
- "Why does the ci take so long? Improve the speed and if hardware is an issue move it to he workstation and make the workstation the default ci/cd runner (if resources are jot fully uses on the server). Does something like a distributed ci/cd docker container setup exist?"
- "Where was the qwen seat run? Should have been via the acp qodercli or qwen-cli. Ovh often times out"

## 2026-10-15 ~18:55Z AskUserQuestion (workstation CI runner), operator verbatim
- Runner host: "Docker desktop but no cpu  cap an 32 gb RAM cap. Additionally do not use github runner anymore for now. Also are there other ci runners freely available? Those can be uses in the future for public repositories like the plangraph"
- Repos: "Use workstation because of speed for now until assumed the repos public and no faster ci is available"
- Parallel CI (xdist + pip cache): "Yes, now (Recommended)"
