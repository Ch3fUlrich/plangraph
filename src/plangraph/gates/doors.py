"""Door and secret path checks for hand-edits and direct_edit routing (D-2835, D-2853)."""
from __future__ import annotations
import fnmatch
from pathlib import Path
import posixpath
from plangraph.gates.privacy import SECRET_DIRS, SECRET_NAME_PATTERNS, secret_path

__all__ = ["AUTH_DOOR_PATHS", "HAND_EDIT_MAX_LINES", "ROUTE_DECISION_LINES",
           "SECRET_DIRS", "SECRET_NAME_PATTERNS", "canon_path", "door_path",
           "blocked_path", "FIXTURE_ROWS"]

AUTH_DOOR_PATHS: tuple[str, ...] = (
    'src/plangraph/keeper/*', 'src/plangraph/*/auth*', 'src/plangraph/*auth*',
    '*/roles*', '*/scopes*', 'roles*', 'scopes*', 'tools/seat_verdict.py*',
    'ops/pg.sh*', 'tools/ci_commit_gate.py*', 'tools/ci_commit_exempt.txt*',
    'tools/gate_wiring_check.py*', '.github/*',
)
HAND_EDIT_MAX_LINES: int = 400
ROUTE_DECISION_LINES: int = 50

def canon_path(path: str | Path) -> str | None:
    p = str(path).replace('\\', '/')
    while p.startswith('./'):
        p = p[2:]
    norm = posixpath.normpath(p)
    if norm.startswith('/') or posixpath.isabs(norm) or any(s == '..' for s in norm.split('/')):
        return None
    return norm

def door_path(path: str | Path) -> bool:
    canon = canon_path(path)
    return canon is not None and any(fnmatch.fnmatchcase(canon, pat) for pat in AUTH_DOOR_PATHS)

def blocked_path(path: str | Path) -> str | None:
    canon = canon_path(path)
    if canon is None:
        return "malformed"
    if door_path(canon):
        return "door"
    if secret_path(canon):
        return "secret"
    return None

FIXTURE_ROWS: tuple[tuple[str, str | None, str], ...] = (
    ("src/plangraph/keeper/x.py", "door", "writer"), ("./src/plangraph/keeper/x.py", "door", "writer"),
    ("src\\plangraph\\keeper\\x.py", "door", "writer"), ("src/plangraph/authoring.md", "door", "writer"),
    ("src/plangraph/keeper/store.py", "door", "writer"), ("src/plangraph/spawner/auth.py", "door", "writer"),
    ("roles.py", "door", "writer"), ("scopes.py", "door", "writer"),
    ("src/roles.py", "door", "writer"), ("src/scopes.py", "door", "writer"),
    ("tools/seat_verdict.py", "door", "writer"), ("ops/pg.sh", "door", "writer"),
    ("tools/ci_commit_gate.py", "door", "writer"), ("./tools/ci_commit_gate.py", "door", "writer"),
    ("tools\\ci_commit_gate.py", "door", "writer"), ("tools/ci_commit_exempt.txt", "door", "writer"),
    ("tools/gate_wiring_check.py", "door", "writer"), (".github/workflows/tests.yml", "door", "writer"),
    (".github/workflows/ci.yml", "door", "writer"), (".github/CODEOWNERS", "door", "writer"),
    ("vault/x.json", "secret", "writer"), ("config/.env", "secret", "writer"),
    (".env", "secret", "writer"), (".env.local", "secret", "writer"),
    ("secrets/prod.json", "secret", "writer"), ("secret/deploy.key", "secret", "writer"),
    (".ssh/id_rsa", "secret", "writer"), (".gnupg/secring.gpg", "secret", "writer"),
    ("keys/server.pem", "secret", "writer"), ("server.key", "secret", "writer"),
    ("client.p12", "secret", "writer"), ("cert.pfx", "secret", "writer"),
    ("db.kdbx", "secret", "writer"), ("app.p8", "secret", "writer"),
    ("keystore.jks", "secret", "writer"), ("credentials.json", "secret", "writer"),
    ("api_keys.json", "secret", "writer"), ("service-account.json", "secret", "writer"),
    ("../x.py", "malformed", "writer"), ("/etc/x", "malformed", "writer"),
    ("a/../../x.py", "malformed", "writer"), ("/var/log/x.txt", "malformed", "writer"),
    ("../", "malformed", "writer"),
    ("docs/tokens.md", "secret", "writer"), ("docs/oauth.md", None, "direct_edit"),
    ("tools/token_count.py", "secret", "writer"), ("docs/keys.md", None, "direct_edit"),
    ("src/plangraph/spawner/cli.py", None, "direct_edit"), ("README.md", None, "direct_edit"),
    ("tests/test_cli.py", None, "direct_edit"), ("src/envelope.py", None, "direct_edit"),
    ("keyboard.py", None, "direct_edit"),
)
