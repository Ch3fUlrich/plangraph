Check commit identities only for history newly introduced to the pushed remote,
excluding its cached main and other tracking refs when main is available. Keep
the strict span when main is missing, the existing email allowlist, and identity
checks on newly created merge commits.
