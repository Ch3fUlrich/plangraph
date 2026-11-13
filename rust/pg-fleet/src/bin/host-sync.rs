fn main() {
    let dirs: Vec<_> =
        std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()).collect();
    let source = std::env::var_os("FLEET_BIN_DIR").map(std::path::PathBuf::from);
    let dest = std::env::var_os("FLEET_PATH_DIR").map(std::path::PathBuf::from);
    let failures = pg_fleet::path_check::check(&dirs, source.as_deref(), dest.as_deref());
    for s in &failures {
        println!("{s}");
    }
    std::process::exit(i32::from(!failures.is_empty()));
}
