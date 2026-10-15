
## 2026-10-15 ~18:35-18:40Z (build-L1 gen-3), operator verbatim
- "Why do you wait for a qwen Seat? Should there be enough parallel runs possible?"
- "Why does the ci take so long? Improve the speed and if hardware is an issue move it to he workstation and make the workstation the default ci/cd runner (if resources are jot fully uses on the server). Does something like a distributed ci/cd docker container setup exist?"
- "Where was the qwen seat run? Should have been via the acp qodercli or qwen-cli. Ovh often times out"

## 2026-10-15 ~18:55Z AskUserQuestion (workstation CI runner), operator verbatim
- Runner host: "Docker desktop but no cpu  cap an 32 gb RAM cap. Additionally do not use github runner anymore for now. Also are there other ci runners freely available? Those can be uses in the future for public repositories like the plangraph"
- Repos: "Use workstation because of speed for now until assumed the repos public and no faster ci is available"
- Parallel CI (xdist + pip cache): "Yes, now (Recommended)"

## 2026-10-15 ~19:02Z AskUserQuestion (L2 cap), operator verbatim
- "What do you more an with "no free L2 slots". There should be plenty available"
- Answer: "Rais cap on both machines. Coding vm limited by 1gb free ram and workstation by 12gb free ram"

## 2026-10-15 ~19:10Z operator verbatim
- "To reduce claude usage also start using mimo-v2.6-flash and free models like opencode/muse-spark-1.3-free or openrouter  subagents for implementations.  If everything works fine with some implementations of the plangraph  we can also test some setups in production"
