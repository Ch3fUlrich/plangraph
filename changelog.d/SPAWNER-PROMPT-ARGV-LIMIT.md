- D-5233: deliver every spawner harness/helper prompt by stdin or staged file,
  including Linux OOM helpers and pg-spawnd launch specs, with no size-dependent
  argv fallback. Reviews exceeding the leg request budget record `prompt_too_large`.
- Review fixes exercise the real remote CLI parser and Python launch-spec producer,
  isolate real-exec transport tests from host routing/probes, strip known task env
  carriers (not unrelated theme/template settings), and preserve the launch adapter's
  refusal exit status in saved records. Pre-exec size refusals need no exit code;
  malformed stdin transports fail with `stdin_prompt_flag_missing`, never a generic error.
- Merge main's source-redaction contract and line-set deletions; keep probe leak
  reasons stable while retaining stderr evidence separately. Remote CLI tests run
  this checkout with site-packages disabled, not an ambient package installation.
- Fix-round regressions cover persisted stderr keys, base/routed service transport
  refusals, legacy-client stdin, the actual diff/bound exception hierarchy and exact
  advisory warnings. Rust accepts incidental short prompt substrings and retains
  plan, attempts and diagnostic evidence for named pre-exec failures.
- Keep large-prompt real-child transport proofs readable after output redaction
  by checking byte length and the full SHA-256 in colon-separated groups.
