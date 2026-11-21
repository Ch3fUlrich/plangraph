A review prompt for a toolless inline seat (catalog `tool_mode` `inline-only`, for example
mimo-v2.6-pro) now starts with a role header that names the tool mode and carries no
instruction to run tests or open files; `skills/review` gains an "Inline seats" section
(validated = quoted diff line with `file:line`, CI evidence named instead of test runs, the
verdict is always given). Tests cover the inline prompt, a fixture inline reply that parses
to a verdict and the oversize inline diff refusal. Not done here: ending an inline run after
3 failed tool calls or 10 minutes without a verdict (the existing mimo review wall stays
1500 s, pinned by tests).
