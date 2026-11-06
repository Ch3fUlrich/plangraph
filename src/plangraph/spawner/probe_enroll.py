"""Enroll a probe host without evaluating operator configuration."""
import os, pathlib, re, shlex, stat, sys, tempfile
if os.name != "posix": sys.exit("Probe enrollment requires POSIX pwsh; native Windows is unsupported")
host, name, dry = sys.argv[1:]
valid = lambda h: re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", h) is not None
if not (valid(host) and name): sys.exit("paired probe host/config required; invalid host")
path = pathlib.Path(name)
if path.is_symlink(): sys.exit("config must not be a symlink")
if not path.is_file(): sys.exit("config must exist and be private")
mode = path.stat().st_mode
if mode & 0o077: sys.exit("config must exist and be private")
text = path.read_bytes().decode("utf-8")
lines = text.splitlines(keepends=True)
entries = [(i, line.split("=", 1)[1].strip()) for i, line in enumerate(lines)
           if re.match(r"^\s*(?:export\s+)?PLANGRAPH_PROBE_HOSTS\s*=", line)]
if len(entries) != 1: sys.exit("exactly one hosts assignment required")
index, value = entries[0]
match = re.fullmatch(r"(\s*(?:export\s+)?PLANGRAPH_PROBE_HOSTS\s*=\s*)([\x27\"]?)([A-Za-z0-9._\- \t]*)\2([ \t]*(?:#[^\r\n]*)?)(\r?\n)?", lines[index])
if not match: sys.exit("invalid hosts assignment")
prefix, quote, value, comment, ending = match.groups()
hosts = shlex.split(value)
if len(hosts) == 1: hosts = hosts[0].split()
if not all(valid(h) for h in hosts): sys.exit("invalid configured host")
if host in hosts: sys.exit(0)
if dry == "1": print("Would add probe host " + host); sys.exit(0)
quote = quote or chr(39)
lines[index] = prefix + quote + " ".join(hosts + [host]) + quote + comment + (ending or "")
fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
try:
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), stat.S_IMODE(mode)); stream.write("".join(lines).encode("utf-8"))
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary): os.unlink(temporary)
