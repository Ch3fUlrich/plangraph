pub(crate) fn excluded(name: &std::ffi::OsStr) -> bool {
    matches!(
        name.to_str(),
        Some(".git-credentials" | ".gitconfig" | ".ssh")
    )
}
