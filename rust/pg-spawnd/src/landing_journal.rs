//! Delivery of one landing journal row through the `plangraph landing journal` command.

use serde_json::Value;
use std::{
    io::Write,
    process::{Command, Stdio},
};

/// Send one journal row; an error means it stays pending for a later delivery.
pub fn deliver(row: &Value) -> Result<(), String> {
    let mut child = Command::new("plangraph")
        .args(["landing", "journal"])
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .spawn()
        .map_err(|err| err.to_string())?;
    let written = child
        .stdin
        .take()
        .ok_or("journal stdin missing")?
        .write_all(row.to_string().as_bytes());
    let status = child.wait().map_err(|err| err.to_string())?;
    if written.is_ok() && status.success() {
        Ok(())
    } else {
        Err(format!("landing journal exited with {status}"))
    }
}
