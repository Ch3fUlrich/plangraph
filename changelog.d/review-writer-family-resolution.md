Resolve Codex, GPT and registered writer leg/model names through one cross-family
resolver. Preserve OpenAI and DeepSeek review fallbacks for other-family writers,
and reject unknown writer names with accepted names. Carry the resolved writer
family into gateway headers from Python review seats and native initial/retry
launches, refusing missing or placeholder authors before launch.
Keep accepted names visible through native refusal limits and resolve writer
aliases before forwarding verdicts to the keeper.
Native refusal lines now use the last non-empty adapter stderr line, so host banners no longer hide the accepted names.
