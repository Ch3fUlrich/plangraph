use std::{fs, io, process::Command, time::Duration};

pub(super) fn run() -> i32 {
    match check_protoc() {
        Ok(()) => {
            println!("OK protoc google/protobuf/field_mask.proto");
            0
        }
        Err(error) => {
            println!("FAIL protoc google/protobuf/field_mask.proto: {error}");
            1
        }
    }
}

fn check_protoc() -> io::Result<()> {
    let scratch = tempfile::tempdir()?;
    let name = "include_probe.proto";
    fs::write(
        scratch.path().join(name),
        concat!(
            "syntax = \"proto3\";\n",
            "import \"google/protobuf/field_mask.proto\";\n",
            "message IncludeProbe { google.protobuf.FieldMask mask = 1; }\n"
        ),
    )?;
    let mut command = Command::new(
        std::env::var_os("PROTOC")
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| "protoc".into()),
    );
    command.current_dir(scratch.path()).args(["-I", "."]);
    if let Some(include) = std::env::var_os("PROTOC_INCLUDE").filter(|value| !value.is_empty()) {
        command.arg("-I").arg(std::path::absolute(include)?);
    }
    command.args(["--descriptor_set_out=probe.pb", name]);
    pg_worker_core::host_command::output(&mut command, Duration::from_secs(10))?;
    if fs::metadata(scratch.path().join("probe.pb"))?.len() == 0 {
        return Err(io::Error::other("empty descriptor"));
    }
    Ok(())
}
