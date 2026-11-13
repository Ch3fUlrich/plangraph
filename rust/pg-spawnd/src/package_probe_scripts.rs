//! Exact-session probes; no process-name or command-line pattern scans.
use crate::{
    package::Host,
    package_scripts::{ps, sh},
};
use serde_json::Value;
fn s<'a>(r: &'a Value, k: &str) -> &'a str {
    r[k].as_str().unwrap_or("")
}
pub(crate) fn script(host: &Host, r: &Value) -> String {
    let base_path = format!("{}/.package-base", s(r, "worktree"));
    if host.kind == "windows" {
        format!(
            r#"$ErrorActionPreference='Stop'
Set-Location {work}
$process=Get-Process -Id {pid} -ErrorAction SilentlyContinue
$alive=($null -ne $process -and $process.StartTime.ToUniversalTime().ToString('o') -eq {started})
$log=Get-Item -LiteralPath {log} -ErrorAction SilentlyContinue
$status=if (Test-Path -LiteralPath {status}) {{ (Get-Content -LiteralPath {status} -Tail 200) -join "`n" }} else {{ '' }}
$tail=if ($log) {{ (Get-Content -LiteralPath {log} -Tail 100) -join "`n" }} else {{ '' }}
$base=(Get-Content -LiteralPath {base}).Trim()
$commits=(& git rev-list --count "$base..HEAD")
if ($LASTEXITCODE -ne 0) {{ throw 'commit probe failed' }}
$task=(& schtasks /Query /TN {task} /FO LIST) -join "`n"
$os=Get-CimInstance Win32_OperatingSystem
$cpu=Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average
$disk=Get-PSDrive -Name ([IO.Path]::GetPathRoot((Get-Location).Path).TrimEnd(':\'))
@{{alive=$alive;agent_status=$(if ($alive) {{'working'}} else {{'dead'}});log_size=$(if ($log) {{$log.Length}} else {{0}});log_mtime=$(if ($log) {{[DateTimeOffset]$log.LastWriteTimeUtc | ForEach-Object {{$_.ToUnixTimeSeconds()}}}} else {{0}});log_tail=$tail;status_text=$status;commits=[int]$commits;task_state=$task;load=$cpu.Average;ram_available_mb=($os.FreePhysicalMemory/1024);disk_free_mb=($disk.Free/1MB)}} | ConvertTo-Json -Compress
"#,
            work = ps(s(r, "worktree")),
            pid = r["pid"].as_u64().unwrap_or(0),
            started = ps(s(r, "started_process")),
            log = ps(s(r, "log_path")),
            status = ps(s(r, "status_path")),
            base = ps(&base_path),
            task = ps(s(r, "task"))
        )
    } else {
        format!(
            r#"set -eu
cd {work}
hex() {{ od -An -tx1 | tr -d ' \n'; }}
log={log}
tmp="$log.probe.$$"
herdr pane read --lines 200 {pane} | head -c 65536 > "$tmp"
if ! cmp -s "$tmp" "$log"; then mv "$tmp" "$log"; else rm "$tmp"; fi
agents=$(herdr agent list | hex)
size=$(wc -c < "$log" | tr -d ' ')
mtime=$(stat -c %Y "$log")
tail_hex=$(tail -c 65536 "$log" | hex)
status_hex=$(if [ -f {status} ]; then tail -n 200 {status} | head -c 65536; fi | hex)
base=$(cat {base})
commits=$(git rev-list --count "$base..HEAD")
load=$(cut -d ' ' -f1 /proc/loadavg)
ram=$(awk '/MemAvailable:/ {{printf "%d",$2/1024}}' /proc/meminfo)
disk=$(df -Pm . | awk 'NR==2 {{print $4}}')
printf '{{"agents_hex":"%s","log_size":%s,"log_mtime":%s,"log_hex":"%s","status_hex":"%s","commits":%s,"load":%s,"ram_available_mb":%s,"disk_free_mb":%s}}\n' "$agents" "$size" "$mtime" "$tail_hex" "$status_hex" "$commits" "$load" "$ram" "$disk"
"#,
            work = sh(s(r, "worktree")),
            log = sh(s(r, "log_path")),
            pane = sh(s(r, "pane")),
            status = sh(s(r, "status_path")),
            base = sh(&base_path)
        )
    }
}
