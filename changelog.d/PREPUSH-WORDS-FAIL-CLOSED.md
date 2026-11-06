### Fixed
- The pre-push hook loads the default private scrub list for public remotes, refuses
  insecure default permissions and invalid explicit lists, and warns when no default exists.
  Locally marked private remotes skip private matching; public patterns always run.
