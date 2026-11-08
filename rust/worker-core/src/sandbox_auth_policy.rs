// Worker control-plane credentials must never be staged as agent login state,
// including when they are tucked inside a CLI configuration subdirectory.
fn worker_credential_name(name: &std::ffi::OsStr) -> bool {
    let name = name.to_string_lossy().to_ascii_lowercase();
    ["pg_", "temporal", "keeper", "tls"]
        .iter()
        .any(|prefix| name.starts_with(prefix))
}

fn symlink_refused(path: &Path) -> std::io::Error {
    std::io::Error::new(
        std::io::ErrorKind::InvalidInput,
        format!(
            "{}: harness auth entries must not be symlinks",
            path.display()
        ),
    )
}
