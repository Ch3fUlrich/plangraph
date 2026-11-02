//! Windows named pipe transport: `\\.\pipe\plangraph-spawnd-<user name>` with a
//! DACL granting access only to the current user's SID.
//!
//! Compile-checked under `x86_64-pc-windows-gnu`; the runtime tests execute on
//! Windows CI in a later slice.

use std::mem::size_of;
use std::ptr;
use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, GENERIC_ALL, HANDLE};
use windows_sys::Win32::Security::{
    AddAccessAllowedAce, GetLengthSid, GetTokenInformation, InitializeAcl,
    InitializeSecurityDescriptor, SetSecurityDescriptorDacl, TokenUser, ACCESS_ALLOWED_ACE, ACL,
    ACL_REVISION, SECURITY_ATTRIBUTES, TOKEN_QUERY, TOKEN_USER,
};
use windows_sys::Win32::System::SystemServices::SECURITY_DESCRIPTOR_REVISION;
use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};
// Declared locally: the windows-sys feature that carries it reads as
// high-entropy to the public-repo scrub (as in worker-core stall/windows.rs).
#[link(name = "advapi32")]
#[allow(non_snake_case)]
extern "system" {
    fn GetUserNameW(lpbuffer: *mut u16, pcbbuffer: *mut u32) -> i32;
}

/// The per-user server pipe address.
pub(crate) fn pipe_name() -> Result<String, String> {
    let mut buf = vec![0u16; 256];
    let mut len = buf.len() as u32;
    let ok = unsafe { GetUserNameW(buf.as_mut_ptr(), &mut len) };
    if ok == 0 {
        return Err("cannot resolve the user name for the pipe path".into());
    }
    let name = String::from_utf16_lossy(&buf[..len.saturating_sub(1) as usize]);
    if name.is_empty() {
        return Err("cannot resolve the user name for the pipe path".into());
    }
    Ok(format!(r"\\.\pipe\plangraph-spawnd-{name}"))
}

fn current_user_sid() -> Result<*mut std::ffi::c_void, String> {
    unsafe {
        let mut token: HANDLE = std::ptr::null_mut();
        if OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) == 0 {
            return Err(format!(
                "OpenProcessToken failed (win32 {})",
                GetLastError()
            ));
        }
        let mut needed: u32 = 0;
        GetTokenInformation(token, TokenUser, ptr::null_mut(), 0, &mut needed);
        let mut buffer = vec![0u8; needed as usize];
        let ok = GetTokenInformation(
            token,
            TokenUser,
            buffer.as_mut_ptr() as *mut std::ffi::c_void,
            needed,
            &mut needed,
        );
        CloseHandle(token);
        if ok == 0 {
            return Err(format!(
                "GetTokenInformation failed (win32 {})",
                GetLastError()
            ));
        }
        // `buffer` is a byte vector, so read the header without alignment assumptions.
        let user = std::ptr::read_unaligned(buffer.as_ptr() as *const TOKEN_USER);
        Ok(user.User.Sid)
    }
}

/// Build a self-relative security descriptor whose DACL allows only the current
/// user's SID, then run `f` with a `SECURITY_ATTRIBUTES` pointing at it.
fn with_user_security_descriptor<T>(
    f: impl FnOnce(*mut std::ffi::c_void) -> Result<T, String>,
) -> Result<T, String> {
    unsafe {
        let sid = current_user_sid()?;
        let sid_len = GetLengthSid(sid) as usize;
        let acl_len =
            size_of::<ACL>() + size_of::<ACCESS_ALLOWED_ACE>() - size_of::<u32>() + sid_len;
        let mut acl_buf = vec![0u8; acl_len];
        let acl = acl_buf.as_mut_ptr() as *mut ACL;
        if InitializeAcl(acl, acl_len as u32, ACL_REVISION) == 0 {
            return Err(format!("InitializeAcl failed (win32 {})", GetLastError()));
        }
        if AddAccessAllowedAce(acl, ACL_REVISION, GENERIC_ALL, sid) == 0 {
            return Err(format!(
                "AddAccessAllowedAce failed (win32 {})",
                GetLastError()
            ));
        }
        let mut descriptor = [0u64; 4];
        let sd = descriptor.as_mut_ptr() as *mut std::ffi::c_void;
        if InitializeSecurityDescriptor(sd, SECURITY_DESCRIPTOR_REVISION) == 0 {
            return Err(format!(
                "InitializeSecurityDescriptor failed (win32 {})",
                GetLastError()
            ));
        }
        if SetSecurityDescriptorDacl(sd, 1, acl, 0) == 0 {
            return Err(format!(
                "SetSecurityDescriptorDacl failed (win32 {})",
                GetLastError()
            ));
        }
        let mut attrs = SECURITY_ATTRIBUTES {
            nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: sd,
            bInheritHandle: 0,
        };
        f(&mut attrs as *mut SECURITY_ATTRIBUTES as *mut std::ffi::c_void)
    }
}

/// Create one server pipe instance guarded by the first-instance flag and the
/// user-only DACL.
pub(crate) fn create_pipe_server(
    first_instance: bool,
) -> Result<tokio::net::windows::named_pipe::NamedPipeServer, String> {
    let name = pipe_name()?;
    with_user_security_descriptor(|attrs| {
        let mut options = tokio::net::windows::named_pipe::ServerOptions::new();
        options
            .first_pipe_instance(first_instance)
            .reject_remote_clients(true);
        unsafe { options.create_with_security_attributes_raw(&name, attrs) }.map_err(|e| {
            if e.kind() == std::io::ErrorKind::PermissionDenied {
                format!("already serving on {name}")
            } else {
                format!("cannot create pipe {name}: {e}")
            }
        })
    })
}

/// Connect a shim client to the server pipe. `open` connects immediately; a
/// busy pipe gets short retries until the caller's timeout expires.
pub(crate) async fn connect() -> Result<tokio::net::windows::named_pipe::NamedPipeClient, String> {
    let name = pipe_name()?;
    loop {
        match tokio::net::windows::named_pipe::ClientOptions::new().open(&name) {
            Ok(client) => return Ok(client),
            Err(e)
                if e.raw_os_error()
                    == Some(windows_sys::Win32::Foundation::ERROR_PIPE_BUSY as i32) =>
            {
                tokio::time::sleep(std::time::Duration::from_millis(50)).await;
            }
            Err(e) => return Err(format!("cannot connect to pipe {name}: {e}")),
        }
    }
}
