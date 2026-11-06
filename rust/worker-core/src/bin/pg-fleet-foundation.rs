use pg_worker_core::fleet_foundation_tick::invoke;
use serde_json::json;

fn main() {
    #[cfg(target_os = "linux")]
    let result = (|| {
        let mut time = libc::timespec {
            tv_sec: 0,
            tv_nsec: 0,
        };
        if unsafe { libc::clock_gettime(libc::CLOCK_BOOTTIME, &mut time) } != 0 {
            return Err("clock read failed".to_owned());
        }
        let wall = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_err(|_| "wall clock failed")?
            .as_secs();
        invoke(
            &std::env::args().skip(1).collect::<Vec<_>>(),
            std::env::var_os("PG_FLEET_FOUNDATION_CONFIG").map(Into::into),
            time.tv_sec as u64 * 1000 + time.tv_nsec as u64 / 1_000_000,
            wall as i64,
        )
    })();
    #[cfg(not(target_os = "linux"))]
    let result: Result<serde_json::Value, String> = Err("unsupported clock target".into());
    let (line, code) = match result {
        Ok(v) => (v, 0),
        Err(e) => (json!({"error":e}), 2),
    };
    println!("{line}");
    std::process::exit(code);
}
