### Fixed

- Remote `ps` hosts can set `win_src` (for example, `C:/opt/plangraph/src`)
  to use Windows Python and read the Windows run store natively within the
  existing query timeout. The cmd.exe command uses a strictly validated absolute
  Windows source path; `win_src` cannot be combined with `src` or `wsl_distro`.
  Existing POSIX and WSL host commands are unchanged (D-4602).
