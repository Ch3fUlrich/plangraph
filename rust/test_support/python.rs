use std::{env, path::Path, process::Command};

/// Pick the checkout's venv or a system Python and replace inherited Python settings.
pub fn command() -> Command {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .find(|dir| dir.join("src/plangraph").is_dir())
        .expect("crate must belong to a plangraph checkout");
    let (bin, venv_python, system_python) = if cfg!(windows) {
        ("Scripts", "python.exe", "python.exe")
    } else {
        ("bin", "python", "python3")
    };
    let inherited_bin = env::var_os("VIRTUAL_ENV").map(|dir| Path::new(&dir).join(bin));
    let inherited_path = env::var_os("PATH").unwrap_or_default();
    let mut path: Vec<_> = env::split_paths(&inherited_path)
        .filter(|entry| {
            entry.is_absolute()
                && Some(entry) != inherited_bin.as_ref()
                && !entry
                    .parent()
                    .is_some_and(|parent| parent.join("pyvenv.cfg").is_file())
        })
        .collect();
    let venv = root.join(".venv");
    let local_python = venv.join(bin).join(venv_python);
    let use_venv = local_python.is_file();
    let executable = if use_venv {
        path.insert(0, venv.join(bin));
        local_python
    } else {
        path.iter()
            .map(|dir| dir.join(system_python))
            .find(|file| file.is_file())
            .expect("Python is required for this test")
    };
    let mut command = Command::new(executable);
    command
        .env(
            "PATH",
            env::join_paths(path).expect("valid executable search path"),
        )
        .env("PYTHONPATH", root.join("src"))
        .env_remove("VIRTUAL_ENV")
        .env_remove("PYTHONHOME");
    if use_venv {
        command.env("VIRTUAL_ENV", venv);
    }
    command
}
