## CI-PREFLIGHT-LOWS

- Add committed-only `--head` comparisons to `tools/affected_tests.py`;
  untracked, staged, and worktree-only changes remain excluded when supplied.
- Verify explicit endpoints and local-change exclusion in a temporary Git repo.
- Open issue: `tests/test_preflight.py` is absent from origin/main; its
  `PREFLIGHT_TOOLS_DIR` fixture isolation awaits CI-PREFLIGHT (#702).
- Open issue: origin/main's `tools/check_file_size.py:measure` reads checkout
  files directly and has no `cat-file --batch` header parser. The malformed
  header refusal and regression test await that implementation from #702.
