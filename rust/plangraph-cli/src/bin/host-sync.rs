use std::{ffi::OsString, fs, io, process::Command, time::Duration};

fn main() {
    if std::env::args_os()
        .skip(1)
        .eq([OsString::from("--check-protoc")])
    {
        match check_protoc() {
            Ok(()) => println!("OK protoc google/protobuf/field_mask.proto"),
            Err(error) => {
                println!("FAIL protoc google/protobuf/field_mask.proto: {error}");
                std::process::exit(1);
            }
        }
        return;
    }
    std::process::exit(plangraph_cli::host_sync::cli_main());
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
