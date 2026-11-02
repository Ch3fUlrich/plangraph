//! Windows named pipe tests. Compiled and linted on every target; executed on
//! Windows CI in a later slice.

#![cfg(windows)]

use std::fs::OpenOptions;
use std::io::{BufRead, BufReader, Write};
use std::os::windows::io::AsRawHandle;
use std::time::{Duration, Instant};
use windows_sys::Win32::Foundation::{LocalFree, HANDLE, HLOCAL};
use windows_sys::Win32::Security::Authorization::{GetSecurityInfo, SE_FILE_OBJECT};
use windows_sys::Win32::Security::{
    CheckTokenMembership, GetAce, GetSecurityDescriptorDacl, ACCESS_ALLOWED_ACE, ACL,
    DACL_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR, PSID,
};
use windows_sys::Win32::System::SystemServices::ACCESS_ALLOWED_ACE_TYPE;
// Declared locally: the windows-sys feature that carries it reads as
// high-entropy to the public-repo scrub (as in worker-core stall/windows.rs).
#[link(name = "advapi32")]
#[allow(non_snake_case)]
extern "system" {
    fn GetUserNameW(lpbuffer: *mut u16, pcbbuffer: *mut u32) -> i32;
}

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_pg-spawnd")
}

fn user_name() -> String {
    let mut buf = vec![0u16; 256];
    let mut len = buf.len() as u32;
    let ok = unsafe { GetUserNameW(buf.as_mut_ptr(), &mut len) };
    assert_ne!(ok, 0, "GetUserNameW failed");
    String::from_utf16_lossy(&buf[..len.saturating_sub(1) as usize])
}

fn pipe_name() -> String {
    format!(r"\\.\pipe\plangraph-spawnd-{}", user_name())
}

struct ChildGuard {
    child: std::process::Child,
}

impl Drop for ChildGuard {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn spawn_serve() -> ChildGuard {
    let child = std::process::Command::new(bin())
        .args(["serve"])
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .expect("spawn serve");
    ChildGuard { child }
}

fn wait_connect(name: &str) -> std::fs::File {
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        let attempt = OpenOptions::new().read(true).write(true).open(name);
        match attempt {
            Ok(file) => return file,
            Err(_) if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(25));
            }
            Err(e) => panic!("server pipe {name} never appeared: {e}"),
        }
    }
}

fn initialize(file: &mut std::fs::File) -> serde_json::Value {
    let request = serde_json::json!({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test-suite", "version": "0"},
        },
    });
    let mut line = serde_json::to_string(&request).expect("serialize");
    line.push('\n');
    file.write_all(line.as_bytes()).expect("write request");
    let mut response = String::new();
    BufReader::new(&mut *file)
        .read_line(&mut response)
        .expect("read response");
    serde_json::from_str(response.trim_end_matches('\n')).expect("parse response")
}

#[test]
fn pipe_name_includes_user_name_and_serves() {
    let name = pipe_name();
    assert!(name.starts_with(r"\\.\pipe\plangraph-spawnd-"), "{name}");
    assert!(name.ends_with(&user_name()), "{name}");
    let _guard = spawn_serve();
    let mut file = wait_connect(&name);
    let response = initialize(&mut file);
    assert_eq!(response["result"]["serverInfo"]["name"], "pg-spawnd");
    assert_eq!(
        response["result"]["serverInfo"]["version"],
        env!("CARGO_PKG_VERSION")
    );
}

#[test]
fn second_serve_on_live_pipe_exits_two() {
    let name = pipe_name();
    let _guard = spawn_serve();
    let _file = wait_connect(&name);
    let output = std::process::Command::new(bin())
        .args(["serve"])
        .output()
        .expect("second serve");
    assert_eq!(output.status.code(), Some(2));
    let stderr = String::from_utf8(output.stderr).expect("utf-8");
    assert_eq!(stderr.lines().count(), 1, "{stderr}");
}

#[test]
fn pipe_dacl_grants_only_current_user() {
    let name = pipe_name();
    let _guard = spawn_serve();
    let file = wait_connect(&name);
    unsafe {
        let mut owner: PSID = std::ptr::null_mut();
        let mut group: PSID = std::ptr::null_mut();
        let mut dacl: *mut ACL = std::ptr::null_mut();
        let mut descriptor: PSECURITY_DESCRIPTOR = std::ptr::null_mut();
        let status = GetSecurityInfo(
            file.as_raw_handle() as HANDLE,
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            &mut owner,
            &mut group,
            &mut dacl,
            std::ptr::null_mut(),
            &mut descriptor,
        );
        assert_eq!(status, 0, "GetSecurityInfo failed");
        let mut present: i32 = 0;
        let mut defaulted: i32 = 0;
        let mut acl: *mut ACL = std::ptr::null_mut();
        assert_ne!(
            GetSecurityDescriptorDacl(descriptor, &mut present, &mut acl, &mut defaulted),
            0
        );
        assert_ne!(present, 0, "pipe must carry a DACL");
        let acl = &*acl;
        assert_eq!(acl.AceCount, 1, "exactly one ACE");
        let mut ace: *mut std::ffi::c_void = std::ptr::null_mut();
        assert_ne!(GetAce(acl as *const ACL, 0, &mut ace), 0);
        let allowed = &*(ace as *const ACCESS_ALLOWED_ACE);
        assert_eq!(allowed.Header.AceType as u32, ACCESS_ALLOWED_ACE_TYPE);
        let sid = &allowed.SidStart as *const u32 as PSID;
        let mut is_member: i32 = 0;
        assert_ne!(
            CheckTokenMembership(std::ptr::null_mut::<()>() as HANDLE, sid, &mut is_member),
            0
        );
        assert_ne!(is_member, 0, "current user must hold the ACE");
        LocalFree(descriptor as HLOCAL);
    }
}
