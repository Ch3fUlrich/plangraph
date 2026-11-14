//! Scripts sent as stdin, with all caller data quoted as literals.
use crate::package::Host;
use serde_json::Value;
pub(crate) fn sh(s: &str) -> String {
    format!("'{}'", s.replace('\'', "'\"'\"'"))
}
pub(crate) fn ps(s: &str) -> String {
    format!("'{}'", s.replace('\'', "''"))
}
pub(crate) fn launch(host: &Host, run: &Value) -> String {
    if host.kind == "windows" {
        windows(host, run)
    } else {
        linux(host, run)
    }
}
fn s<'a>(v: &'a Value, k: &str) -> &'a str {
    v[k].as_str().unwrap()
}
fn linux(host: &Host, r: &Value) -> String {
    let repo = sh(&host.repo);
    let work = sh(s(r, "worktree"));
    let branch = sh(s(r, "branch"));
    let base = sh(s(r, "base_ref"));
    let id = sh(s(r, "run_id"));
    let package = sh(s(r, "package_path"));
    let text = sh(s(r, "package_text"));
    format!(
        r#"set -eu
cd {repo}
test "$(git symbolic-ref --short HEAD)" = main
test -z "$(git status --porcelain)"
git fetch --no-write-fetch-head origin '+refs/heads/main:refs/remotes/origin/main' {base_spec}
git merge --ff-only --no-edit origin/main
git merge-base --is-ancestor origin/main {base}
test -n "$(git config user.name)"
git config user.email | LC_ALL=C grep -Eq '^([0-9]+\+)?[A-Za-z0-9._-]+@users\.noreply\.github\.com$'
for ident in GIT_AUTHOR_IDENT GIT_COMMITTER_IDENT; do
 git var "$ident" | sed -n 's/.*<\([^>]*\)>.*/\1/p' | LC_ALL=C grep -Eq '^([0-9]+\+)?[A-Za-z0-9._-]+@users\.noreply\.github\.com$'
done
mkdir -p {root}
git worktree add -b {branch} {work} {base}
printf '%s' {text} > {package}
git -C {work} rev-parse HEAD > {work}/.package-base
printf '' > {work}/STEER
herdr workspace create --cwd {work} --label {id}
"#,
        root = sh(&host.root),
        base_spec = sh(&format!(
            "+refs/heads/{}:refs/remotes/{}",
            s(r, "base_ref").trim_start_matches("origin/"),
            s(r, "base_ref")
        ))
    )
}
pub(crate) fn start_linux(r: &Value) -> String {
    let pane = sh(s(r, "pane"));
    let agent = sh(s(r, "agent_name"));
    let model = sh(s(r, "model"));
    let effort = sh(&format!("model_reasoning_effort={}", s(r, "effort")));
    let prompt = sh(&format!(
        "Read {} and execute tasks in order. Read {} before each task for steering. Do not push.",
        s(r, "package_path"),
        s(r, "steer_path")
    ));
    format!(
        r#"set -eu
if ! herdr agent start {agent} --kind codex --pane {pane} --timeout 90000 -- -m {model} -c {effort} -c approval_policy=never --sandbox danger-full-access; then
 screen=$(herdr pane read {pane})
 case "$screen" in *'Skip until next version'*|*'continue without trusting'*) ;; *) exit 1;; esac
fi
n=0
while [ "$n" -lt 20 ]; do
 screen=$(herdr pane read {pane})
 case "$screen" in *'Skip until next version'*|*'continue without trusting'*) herdr pane send-keys {pane} Down Down Enter ;; esac
 n=$((n+1)); sleep 1
done
herdr pane send-text {pane} {prompt}
herdr pane send-keys {pane} Enter
printf '%s\n' '{{"alive":true}}'
"#
    )
}
fn windows(host: &Host, r: &Value) -> String {
    let runner=format!(r#"$ErrorActionPreference='Stop'
Set-Location {work}
[IO.File]::WriteAllText({pid}, "$PID")
& codex exec -m {model} -c {effort} --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check {prompt} *> {log}
$code=$LASTEXITCODE
Add-Content -LiteralPath {log} -Value "EXIT $code"
exit $code
"#,work=ps(s(r,"worktree")),pid=ps(s(r,"pid_path")),model=ps(s(r,"model")),effort=ps(&format!("model_reasoning_effort={}",s(r,"effort"))),log=ps(s(r,"log_path")),prompt=ps(&format!("Read {} and execute tasks in order. Read {} before each task for steering. Do not push.",s(r,"package_path"),s(r,"steer_path"))));
    format!(
        r#"$ErrorActionPreference='Stop'
function Git {{ & git @args; if ($LASTEXITCODE -ne 0) {{ throw 'git failed' }} }}
Set-Location {repo}
if ((Git symbolic-ref --short HEAD) -ne 'main') {{ throw 'main checkout required' }}
if (Git status --porcelain) {{ throw 'dirty main checkout' }}
Git fetch --no-write-fetch-head origin '+refs/heads/main:refs/remotes/origin/main' {spec}
Git merge --ff-only --no-edit origin/main
Git merge-base --is-ancestor origin/main {base}
if (!(Git config user.name)) {{ throw 'git identity missing' }}
if ((Git config user.email) -cnotmatch '^([0-9]+\+)?[A-Za-z0-9._-]+@users\.noreply\.github\.com$') {{ throw 'git identity refused' }}
foreach ($ident in @('GIT_AUTHOR_IDENT','GIT_COMMITTER_IDENT')) {{
 if ((Git var $ident) -cnotmatch '<([0-9]+\+)?[A-Za-z0-9._-]+@users\.noreply\.github\.com>') {{ throw 'effective git identity refused' }}
}}
New-Item -ItemType Directory -Force -Path {root} | Out-Null
Git worktree add -b {branch} {work} {base}
[IO.File]::WriteAllText({package}, {text})
Git -C {work} rev-parse HEAD | Set-Content -LiteralPath (Join-Path {work} .package-base)
[IO.File]::WriteAllText((Join-Path {work} STEER), '')
[IO.File]::WriteAllText({runner_path}, {runner})
{schedule}
Start-Sleep -Seconds 30
$launchedPid=[int](Get-Content -LiteralPath {pid})
$process=Get-Process -Id $launchedPid -ErrorAction SilentlyContinue
if (!$process) {{ throw 'detached package died' }}
@{{pid=$launchedPid;task={task};alive=$true;started_process=$process.StartTime.ToUniversalTime().ToString('o')}} | ConvertTo-Json -Compress
"#,
        schedule = crate::package_windows::commands(s(r,"run_id"),s(r,"runner_path")).into_iter()
            .map(|args|format!("& schtasks {} | Out-Null\nif ($LASTEXITCODE -ne 0) {{ throw 'scheduled task failed' }}",args.iter().map(|s|ps(s)).collect::<Vec<_>>().join(" ")))
            .collect::<Vec<_>>().join("\n"),
        repo = ps(&host.repo),
        root = ps(&host.root),
        base = ps(s(r, "base_ref")),
        spec = ps(&format!(
            "+refs/heads/{}:refs/remotes/{}",
            s(r, "base_ref").trim_start_matches("origin/"),
            s(r, "base_ref")
        )),
        branch = ps(s(r, "branch")),
        work = ps(s(r, "worktree")),
        package = ps(s(r, "package_path")),
        text = ps(s(r, "package_text")),
        runner_path = ps(s(r, "runner_path")),
        runner = ps(&runner),
        task = ps(s(r, "run_id")),
        pid = ps(s(r, "pid_path"))
    )
}
pub(crate) fn send(host: &Host, run: &Value, text: &str) -> String {
    if host.kind == "windows" {
        format!("$ErrorActionPreference='Stop'\nAdd-Content -LiteralPath {} -Value {}\n'{{\"ok\":true}}'\n",ps(s(run,"steer_path")),ps(text))
    } else {
        format!("set -eu\nherdr pane send-text {} {}\nherdr pane send-keys {} Enter\nprintf '%s\\n' '{{\"ok\":true}}'\n",sh(s(run,"pane")),sh(text),sh(s(run,"pane")))
    }
}
