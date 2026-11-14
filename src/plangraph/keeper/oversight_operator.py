"""R8 operator proof, exact host guards, and stale important gates."""
from __future__ import annotations

import re


def guard(host, shell="sh"):
    """Return the canonical opening guard; reject shell metacharacters."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", host):
        raise ValueError("invalid host name")
    message = f"WRONG HOST: run this on {host}"
    if shell == "sh":
        return (f'if [ "$(hostname -s)" != "{host}" ]; then\n'
                f'  echo "{message}"\nelse\n', "fi")
    if shell == "pwsh":
        return (f'if ($env:COMPUTERNAME -cne "{host}") {{\n'
                f'  Write-Output "{message}"\n}} else {{\n', "}")
    raise ValueError("invalid shell")


def render(host, tested, commands, shell="sh"):
    if not re.fullmatch(r"\S+[^\S\n]+\S[^\n]*", tested):
        raise ValueError("tested requires HOST EVIDENCE on one line")
    opening, closing = guard(host, shell)
    return (f"host: {host}\ntested: {tested}\n```{shell}\n" + opening
            + commands.rstrip() + "\n" + closing + "\n```\n")


def command_blocks(text):
    """Extract fenced commands and prompt lines, preserving their order."""
    return [body for shell, body in _step_parts(text)[0]]


def _step_parts(text):
    blocks = []
    outside = []
    current = None
    fence = None
    for line in text.splitlines():
        match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if match:
            if current is None:
                current, fence = [], match[1][0]
                shell = line[match.end():].strip()
            elif match[1][0] == fence:
                blocks.append((shell, "\n".join(current) + "\n"))
                current = None
            continue
        if current is not None:
            current.append(line)
        else:
            outside.append(line)
            if line.startswith(("$ ", "! ")):
                blocks.append((None, line[2:] + "\n"))
    if current is not None:
        blocks.append((shell, "\n".join(current) + "\n"))
    return blocks, "\n".join(outside)


def r8_operator(conn, now):
    from .oversight import _age, _finding, _last_events, _sort
    from .stall import _CLOSED

    try:
        items = {row["key"]: dict(row) for row in conn.execute(
            'SELECT key, kind, state, operator, priority, description AS "desc", owner,'
            ' updated_at, created_at FROM items') if row["state"] not in _CLOSED}
        successors = {}
        for row in conn.execute("SELECT src, dst FROM edges WHERE rel='depends_on'"):
            if row["src"] in items and row["dst"] in items:
                successors.setdefault(row["dst"], []).append(row["src"])
        events = _last_events(conn)
        rows = []
        for key, item in items.items():
            if not (item["operator"] or item["kind"] == "operator_task"):
                continue
            text = item["desc"] or ""
            blocks, metadata = _step_parts(text)
            stamp = events.get(key) or item["updated_at"] or item["created_at"]
            def finding(kind, detail):
                rows.append(_finding("R8", kind, key, detail, stamp))
            if blocks and not re.search(r"^tested:[ \t]+\S+[ \t]+\S[^\n]*$", metadata, re.M):
                finding("operator_step_untested", f"operator step {key} lacks host probe evidence")
            host = re.search(r"^host:[ \t]+(\S+)[ \t]*$", metadata, re.M)
            if host:
                valid = False
                for shell, body in blocks[:1]:
                    try:
                        opening, closing = guard(host[1], shell)
                    except ValueError:
                        continue
                    valid = (body.startswith(opening)
                             and body.rstrip().endswith("\n" + closing))
                if not valid:
                    finding("operator_step_no_guard", f"operator step {key} lacks guard for {host[1]}")
            age = _age(stamp, now)
            if item["kind"] != "operator_task" or age is None or age < 86400:
                continue
            seen, todo = {key}, list(successors.get(key, ()))
            gated = []
            while todo:
                target = todo.pop()
                if target in seen:
                    continue
                seen.add(target)
                candidate = items[target]
                # D-5840: high priority on the bounded 0-10 scale is 9-10.
                if candidate["priority"] >= 9:
                    gated.append(candidate)
                todo.extend(successors.get(target, ()))
            if gated:
                highest = sorted(gated, key=lambda it: (-it["priority"], it["key"]))[0]
                finding("operator_step_stale", f"operator {key} gates {highest['key']} owner {highest['owner']}")
        return _sort(rows)
    except Exception as exc:  # a failed snapshot is not a clean pass
        return [_finding("R8", "oversight_check_failed", "r8", type(exc).__name__, "")]
