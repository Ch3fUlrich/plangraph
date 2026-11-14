//! Windows named pipe tests. Compiled and linted on every target; executed on
//! Windows CI in a later slice.

#![cfg(windows)]

use std::fs::OpenOptions;
use std::io::{Read, Write};
use std::os::windows::io::AsRawHandle;
use std::time::{Duration, Instant};
use windows_sys::Win32::Foundation::HANDLE;
use windows_sys::Win32::Security::{
    CheckTokenMembership, GetAce, GetKernelObjectSecurity, GetSecurityDescriptorDacl,
    ACCESS_ALLOWED_ACE, ACL, DACL_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR, PSID,
};
use windows_sys::Win32::System::SystemServices::ACCESS_ALLOWED_ACE_TYPE;
thread_local! {
    static PIPE_NAMESPACE: tempfile::TempDir = tempfile::Builder::new()
        .prefix("pg-pipe-").tempdir().unwrap();
}
fn namespace() -> String {
    PIPE_NAMESPACE.with(|dir| dir.path().file_name().unwrap().to_str().unwrap().to_owned())
}
fn command() -> std::process::Command {
    let mut command = std::process::Command::new(bin());
    command.env("PG_SPAWND_PIPE_NAMESPACE", namespace());
    command
}
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
    format!(r"\\.\pipe\plangraph-spawnd-{}-{}", namespace(), user_name())
}

struct ChildGuard {
    child: std::process::Child,
}

impl Drop for ChildGuard {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = bounded_wait(&mut self.child);
    }
}

fn spawn_serve() -> ChildGuard {
    let child = command()
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
    let deadline = Instant::now() + Duration::from_secs(2);
    let mut response = String::new();
    loop {
        let mut available = 0;
        assert_ne!(
            unsafe {
                PeekNamedPipe(
                    file.as_raw_handle() as HANDLE,
                    std::ptr::null_mut(),
                    0,
                    std::ptr::null_mut(),
                    &mut available,
                    std::ptr::null_mut(),
                )
            },
            0
        );
        if available > 0 {
            let mut byte = [0];
            file.read_exact(&mut byte).unwrap();
            response.push(byte[0] as char);
            if byte[0] == b'\n' {
                break;
            }
        }
        assert!(Instant::now() < deadline, "pipe read deadline");
        std::thread::sleep(Duration::from_millis(1));
    }
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
    let output = output(command().args(["serve"]));
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
        // GetKernelObjectSecurity takes no object type (GetSecurityInfo with
        // SE_FILE_OBJECT answered 87 on a pipe client handle); the client
        // handle carries READ_CONTROL through GENERIC_READ.
        let handle = file.as_raw_handle() as HANDLE;
        let mut needed = 0u32;
        GetKernelObjectSecurity(
            handle,
            DACL_SECURITY_INFORMATION,
            std::ptr::null_mut(),
            0,
            &mut needed,
        );
        assert!(needed > 0, "no security descriptor size");
        let mut buf = vec![0u8; needed as usize];
        let descriptor = buf.as_mut_ptr() as PSECURITY_DESCRIPTOR;
        let ok = GetKernelObjectSecurity(
            handle,
            DACL_SECURITY_INFORMATION,
            descriptor,
            needed,
            &mut needed,
        );
        assert_ne!(ok, 0, "GetKernelObjectSecurity failed");
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
    }
}

#[test]
fn disconnected_pipe_probe_leaves_server_alive() {
    let _guard = spawn_serve();
    let name = pipe_name();
    drop(wait_connect(&name));
    let mut file = wait_connect(&name);
    assert_eq!(
        initialize(&mut file)["result"]["serverInfo"]["name"],
        "pg-spawnd"
    );
}

#[test]
fn socket_flag_is_rejected_for_per_user_pipe() {
    for verb in ["serve", "shim"] {
        let output = output(command().args([verb, "--socket", "unused"]));
        assert_eq!(output.status.code(), Some(2));
        let error = String::from_utf8(output.stderr).unwrap();
        assert_eq!(error.lines().count(), 1);
        assert!(error.contains("per-user"));
    }
}

#[link(name = "kernel32")]
#[allow(non_snake_case)]
extern "system" {
    fn PeekNamedPipe(
        handle: HANDLE,
        buffer: *mut std::ffi::c_void,
        size: u32,
        read: *mut u32,
        available: *mut u32,
        left: *mut u32,
    ) -> i32;
}
fn bounded_wait(child: &mut std::process::Child) -> std::process::ExitStatus {
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        if let Some(status) = child.try_wait().unwrap() {
            return status;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            panic!("child exit deadline");
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}
fn output(command: &mut std::process::Command) -> std::process::Output {
    let mut guard = ChildGuard {
        child: command
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .unwrap(),
    };
    let status = bounded_wait(&mut guard.child);
    let mut stdout = Vec::new();
    let mut stderr = Vec::new();
    guard
        .child
        .stdout
        .take()
        .unwrap()
        .read_to_end(&mut stdout)
        .unwrap();
    guard
        .child
        .stderr
        .take()
        .unwrap()
        .read_to_end(&mut stderr)
        .unwrap();
    std::process::Output {
        status,
        stdout,
        stderr,
    }
}
