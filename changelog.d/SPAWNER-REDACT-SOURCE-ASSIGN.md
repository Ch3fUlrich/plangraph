- Never exempt tight ALL_UPPERCASE=value env credentials in Python or Rust; otherwise preserve
  only braced f/b/r-prefixed strings (including rf/fr and triple quotes), direct dotted calls/indexes
  balanced on the same line, and direct ++/-- digit-free identifiers ending at EOL or semicolon.
- Scan preserved source expressions for nested credential assignments; mask plain quoted literals
  while keeping quotes and expression suffixes. Restore multiline colon separator whitespace,
  but keep equals separators on one line. Bare words, operators, containers and !/~ stay masked.
- Require synthetic leak and boundary cases in the shared Python/Rust parity table, keeping
  Python's quote-preserving, glued-quote-tail and placeholder-tail results where Rust differed;
  retain independent secret scrubbing and supported review diff expressions.
