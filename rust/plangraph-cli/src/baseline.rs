//! `plangraph baseline`: hands the arguments to the `pg-telemetry` binary.
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::Command;

/// The program that answers `plangraph baseline`: a `pg-telemetry` installed next to
/// the running `plangraph` wins over the one on `PATH`.
pub fn telemetry_program(current_exe: Option<&Path>) -> PathBuf {
    let name = format!("pg-telemetry{}", std::env::consts::EXE_SUFFIX);
    current_exe
        .and_then(Path::parent)
        .map(|dir| dir.join(&name))
        .filter(|sibling| sibling.is_file())
        .unwrap_or_else(|| PathBuf::from(name))
}

/// Run `pg-telemetry` with the effective environment and return its exit code.
pub fn run(effective: &BTreeMap<String, String>, args: &[String]) -> i32 {
    let program = telemetry_program(std::env::current_exe().ok().as_deref());
    match Command::new(&program).envs(effective).args(args).status() {
        Ok(status) => status.code().unwrap_or(1),
        Err(error) => {
            eprintln!("baseline: cannot start pg-telemetry: {error}");
            1
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_sibling_binary_wins_and_a_missing_one_falls_back_to_the_path_name() {
        let dir = tempfile::tempdir().unwrap();
        let exe = dir.path().join("plangraph");
        assert_eq!(
            telemetry_program(Some(&exe)),
            PathBuf::from(format!("pg-telemetry{}", std::env::consts::EXE_SUFFIX))
        );
        let sibling = dir
            .path()
            .join(format!("pg-telemetry{}", std::env::consts::EXE_SUFFIX));
        std::fs::write(&sibling, "").unwrap();
        assert_eq!(telemetry_program(Some(&exe)), sibling);
        assert_eq!(
            telemetry_program(None),
            PathBuf::from(format!("pg-telemetry{}", std::env::consts::EXE_SUFFIX))
        );
    }
}
