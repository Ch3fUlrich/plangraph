- Fail closed on every Python and Rust credential-assignment value except four source forms:
  prefixed strings containing braces, direct identifier calls/indexes, direct ++/--/!/~ identifier
  prefixes, and quoted literals followed by symbolic operators, attributes or indexes.
- Mask bare/dotted words, spaced/tight operators between bare words, word operators and bare
  containers; keep independent secret-pattern scrubbing and supported review diff expressions.
- Cover seven synthetic leak regressions and source-form boundaries in the required shared
  Python/Rust parity table, including complete container masking and triple-quoted strings.
