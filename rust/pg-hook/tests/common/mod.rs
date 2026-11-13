#![cfg(unix)]

use std::path::{Path, PathBuf};
use std::process::Command;

pub fn python_executable() -> PathBuf {
    // Resolve before clearing the environment; never relocate Python away from its DLLs.
    let output = Command::new("python3")
        .args(["-I", "-c", "import sys; print(sys.executable)"])
        .output()
        .unwrap();
    assert!(output.status.success());
    let path = PathBuf::from(String::from_utf8(output.stdout).unwrap().trim());
    assert!(path.is_absolute());
    path
}

pub fn python(script: &Path) -> Command {
    let mut command = Command::new(python_executable());
    command.arg("-I").arg(script);
    command
}
