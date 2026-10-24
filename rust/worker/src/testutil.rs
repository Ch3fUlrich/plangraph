//! Test helpers shared by the unit tests of this crate.

use std::io::Write;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

/// Write an executable script `dir/name` that is safe to exec at once.
///
/// The content goes to a temp name, is synced and the fd dropped, the mode is
/// set, and only then is it renamed into place. So no open write fd on the
/// final path can meet a concurrent fork+exec in another test thread, which is
/// what produces "Text file busy" (ETXTBSY). Every test that writes a script
/// and later runs it must use this one helper.
pub fn write_script(dir: &Path, name: &str, body: &str) -> PathBuf {
    let path = dir.join(name);
    let tmp = dir.join(format!(".{name}.tmp"));
    {
        let mut f = std::fs::File::create(&tmp).unwrap();
        f.write_all(body.as_bytes()).unwrap();
        f.sync_all().unwrap();
    }
    std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o755)).unwrap();
    std::fs::rename(&tmp, &path).unwrap();
    path
}
