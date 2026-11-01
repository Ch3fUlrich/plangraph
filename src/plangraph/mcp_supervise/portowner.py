"""Which process is listening on a local TCP port (stdlib only)."""

import os
import subprocess
import sys
from typing import Optional, Set

_WILDCARDS = {"0.0.0.0", "[::]", "::"}


def listener_pid(host: str, port: int) -> Optional[int]:
    """Pid of a process listening on `host:port` (or on all interfaces), else None.

    Best effort: None also means "could not tell" (tool missing, not permitted).
    """
    try:
        if sys.platform == "win32":
            return _from_netstat(host, port)
        if os.path.exists("/proc/net/tcp"):
            return _from_proc(host, port)
        return _from_lsof(port)
    except Exception:
        return None


def _from_netstat(host: str, port: int) -> Optional[int]:
    # Match on the 0.0.0.0:0 foreign address, not the LISTENING word: it is localized.
    out = subprocess.run(
        ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=15, check=False
    ).stdout
    for line in out.splitlines():
        cols = line.split()
        if len(cols) != 5 or cols[0].upper() != "TCP" or not cols[4].isdigit():
            continue
        local_host, _, local_port = cols[1].rpartition(":")
        if local_port == str(port) and local_host in ({host} | _WILDCARDS) \
                and cols[2].endswith(":0"):
            return int(cols[4])
    return None


def _hex_addr(host: str) -> str:
    a, b, c, d = (int(p) for p in host.split("."))
    return f"{d:02X}{c:02X}{b:02X}{a:02X}"


def _from_proc(host: str, port: int) -> Optional[int]:
    wanted = {f"{_hex_addr(host)}:{port:04X}", f"00000000:{port:04X}"}
    inodes: Set[str] = set()
    with open("/proc/net/tcp", encoding="ascii") as f:
        for line in list(f)[1:]:
            cols = line.split()
            if len(cols) > 9 and cols[3] == "0A" and cols[1] in wanted:
                inodes.add(cols[9])
    targets = {f"socket:[{i}]" for i in inodes}
    if not targets:
        return None
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        fd_dir = f"/proc/{entry}/fd"
        try:
            for fd in os.listdir(fd_dir):
                if os.readlink(f"{fd_dir}/{fd}") in targets:
                    return int(entry)
        except OSError:
            continue
    return None


def _from_lsof(port: int) -> Optional[int]:
    out = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        capture_output=True, text=True, timeout=15, check=False,
    ).stdout.split()
    return int(out[0]) if out and out[0].isdigit() else None
