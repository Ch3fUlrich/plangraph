"""Private, immutable document transport using git plumbing (stdlib only)."""
import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

# Shape-only rules shared with tools/hooks/redact_common.py; deliberately no pairs.
_SHAPES = tuple(re.compile(p) for p in (
    r"sk[-_][A-Za-z0-9_-]{16,}", r"\bsk[-_][A-Za-z0-9_-]{4,}",
    r"gh[pousr]_[A-Za-z0-9]+", r"github_pat_[A-Za-z0-9_]+",
    r"xox[abprs]-[A-Za-z0-9-]+", r"(?:AKIA|ASIA)[0-9A-Z]{16}",
    r"(?i:Bearer)\s+[A-Za-z0-9._~+/=-]+",
    r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
))
NAME = r"[A-Za-z0-9._-]{1,80}"
REF = rf"artifacts/([0-9a-f]{{12}})-({NAME})"
LINE = (rf"ARTIFACT ({NAME}) sha256:([0-9a-f]{{64}}) lines=([0-9]+) "
        rf"bytes=([0-9]+) ref=({REF})(?: item=([A-Za-z0-9._:-]+))?")
MARKER = "refs/heads/artifact-store"


class Failure(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


def valid_name(name):
    return re.fullmatch(NAME, name) and name not in (".", "..")


def facts(data):
    return hashlib.sha256(data).hexdigest(), data.count(b"\n") + bool(data and not data.endswith(b"\n")), len(data)


class Store:
    def __init__(self, directory, remote):
        self.directory, self.remote = directory, remote
        self.env = dict(os.environ, GIT_TERMINAL_PROMPT="0",
                        GIT_AUTHOR_NAME="fleet-artifact", GIT_COMMITTER_NAME="fleet-artifact",
                        GIT_AUTHOR_EMAIL="fleet-artifact@artifact.invalid",
                        GIT_COMMITTER_EMAIL="fleet-artifact@artifact.invalid")
        self.git("init", "--bare", directory)

    def git(self, *args, data=None, allow=False):
        try:
            result = subprocess.run(["git", "-c", "core.hooksPath=" + str(Path(self.directory) / "no-hooks"),
                                     "--git-dir", self.directory, *args],
                                    input=data, capture_output=True, env=self.env, timeout=120)
        except subprocess.TimeoutExpired:
            raise Failure(3, "fleet-artifact: git timeout") from None
        if result.returncode and not allow:
            # Git diagnostics can quote attacker-controlled tree data. Do not relay them.
            raise Failure(3, "fleet-artifact: git operation failed")
        return result

    def exists(self, ref):
        return bool(self.git("ls-remote", self.remote, ref).stdout.strip())

    def marker(self):
        if not self.exists(MARKER):
            raise Failure(2, "fleet-artifact: missing private-store marker; run fleet-artifact init --confirm-private")

    def commit(self, tree, message):
        return self.git("commit-tree", tree, data=message.encode()).stdout.decode().strip()


def put(store, args):
    store.marker()
    data = Path(args.path).read_bytes()
    if len(data) > 5 * 1024 * 1024:
        raise Failure(6, "fleet-artifact: size exceeds 5 MiB")
    bad = [str(i) for i, line in enumerate(data.decode("latin1").splitlines(), 1)
           if any(p.search(line) for p in _SHAPES)]
    if bad:
        raise Failure(7, "fleet-artifact: secret shape on lines " + ",".join(bad))
    name = args.name
    if name is None:
        name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(args.path).name)[:80]
    if not valid_name(name) or (args.item and not re.fullmatch(r"[A-Za-z0-9._:-]+", args.item)):
        raise Failure(2, "fleet-artifact: invalid name or item")
    digest, lines, size = facts(data)
    ref = f"artifacts/{digest[:12]}-{name}"
    pointer = f"ARTIFACT {name} sha256:{digest} lines={lines} bytes={size} ref={ref}"
    if args.item:
        pointer += f" item={args.item}"
    full = "refs/heads/" + ref
    if not store.exists(full):
        blob = store.git("hash-object", "-w", "--stdin", data=data).stdout.strip()
        tree = store.git("mktree", data=b"100644 blob " + blob + b"\t" + name.encode() + b"\n").stdout.decode().strip()
        commit = store.commit(tree, pointer + "\n")
        result = store.git("push", store.remote, commit + ":" + full, allow=True)
        if result.returncode:
            if not store.exists(full):
                raise Failure(3, "fleet-artifact: push failed")
            load(store, pointer)
    else:
        load(store, pointer)
    print(pointer)


def load(store, pointer):
    store.marker()
    match = re.fullmatch(LINE, pointer)
    if match:
        name, digest, lines, size, ref, prefix, refname, _ = match.groups()
        if name != refname or not digest.startswith(prefix):
            raise Failure(4, "fleet-artifact: invalid pointer")
    else:
        bare = re.fullmatch(REF, pointer)
        if not bare:
            raise Failure(4, "fleet-artifact: invalid pointer")
        prefix, name = bare.groups()
        ref, digest, lines, size = pointer, None, None, None
    if not valid_name(name):
        raise Failure(4, "fleet-artifact: invalid name")
    store.git("fetch", "--depth", "1", store.remote, "refs/heads/" + ref)
    entries = store.git("ls-tree", "-z", "FETCH_HEAD").stdout.split(b"\0")
    if len(entries) != 2 or entries[-1] != b"":
        raise Failure(4, "fleet-artifact: invalid tree")
    meta, sep, filename = entries[0].partition(b"\t")
    fields = meta.split()
    if not sep or filename != name.encode() or len(fields) != 3 or fields[:2] != [b"100644", b"blob"]:
        raise Failure(4, "fleet-artifact: invalid tree")
    data = store.git("cat-file", "blob", fields[2].decode("ascii")).stdout
    actual, count, length = facts(data)
    if (not actual.startswith(prefix) or (digest and actual != digest)
            or (lines is not None and (int(lines) != count or int(size) != length))):
        raise Failure(4, "fleet-artifact: integrity mismatch")
    return name, data, actual, count, length


def get(store, args):
    name, data, actual, count, length = load(store, args.pointer)
    directory = Path(args.out)
    target = directory / name
    if target.exists():
        if target.read_bytes() != data:
            raise Failure(5, "fleet-artifact: destination differs")
    else:
        directory.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as output:
                temporary = Path(output.name)
                os.chmod(temporary, 0o600)
                output.write(data)
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    print(f"OK {target} sha256:{actual} lines={count} bytes={length}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init").add_argument("--confirm-private", action="store_true")
    p = sub.add_parser("put")
    p.add_argument("path")
    p.add_argument("--name")
    p.add_argument("--item")
    g = sub.add_parser("get")
    g.add_argument("pointer")
    g.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        remote = os.environ.get("PLANGRAPH_ARTIFACT_REMOTE")
        if not remote:
            raise Failure(2, "fleet-artifact: PLANGRAPH_ARTIFACT_REMOTE is unset")
        if remote.startswith("-"):
            raise Failure(2, "fleet-artifact: invalid remote")
        if "://" in remote and urlsplit(remote).netloc.find("@") >= 0:
            raise Failure(2, "fleet-artifact: remote URL userinfo is forbidden")
        if args.command == "init" and not args.confirm_private:
            raise Failure(2, "fleet-artifact init requires --confirm-private")
        with tempfile.TemporaryDirectory(prefix="fleet-artifact-") as directory:
            store = Store(directory, remote)
            if args.command == "init":
                if not store.exists(MARKER):
                    tree = store.git("mktree", data=b"").stdout.decode().strip()
                    store.git("push", remote, store.commit(tree, "Private artifact store\n") + ":" + MARKER)
            elif args.command == "put":
                put(store, args)
            else:
                get(store, args)
        return 0
    except Failure as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    except (OSError, ValueError):
        print("fleet-artifact: local IO or invalid input error", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
