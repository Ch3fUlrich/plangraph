pub(crate) fn excluded(name: &std::ffi::OsStr) -> bool {
    // Deliberately name-based at both staging levels for this pilot:
    // bare hosts.yml and credentials also exclude benign files, and .config
    // excludes the entire directory, not just known authentication entries.
    matches!(
        name.to_str(),
        Some(
            ".git-credentials"
                | ".gitconfig"
                | ".ssh"
                | ".netrc"
                | ".npmrc"
                | ".pypirc"
                | ".aws"
                | ".docker"
                | ".gnupg"
                | "hosts.yml"
                | "credentials"
                | ".config"
        )
    )
}
