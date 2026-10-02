#!/usr/bin/env python3
"""Build the public plangraph tree: allow-listed copy + fresh one-commit history.

    make_public.py --source CHECKOUT --out NEWDIR [--manifest tools/split/manifest.toml]
                   [--rules rules.toml] [--words private_words.txt]
                   [--gitleaks PATH | --no-gitleaks] [--date ISO] [--check]

Only paths the manifest allow-lists are copied (the source's ``.git`` is never
read), then the staged tree must pass: no deny-listed path, no symlink,
``scrub_check`` (path:line:rule only) and ``gitleaks`` if installed.  Only then is
a FRESH repository created (one commit, manifest author) and moved to ``--out``.
``--check`` does everything except the final move/commit-keeping: it builds in a
temp dir, reports, deletes.  Exit 0 ok, 1 refused (hits), 2 usage/config error.
No matched value is ever printed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scrub_check as sc  # noqa: E402
from scrub_check import glob_re  # noqa: E402,F401  (re-exported)

DEFAULT_MANIFEST = Path(__file__).resolve().with_name("manifest.toml")


class UsageError(Exception):
    pass


def load_manifest(path):
    try:
        with open(path, "rb") as fh:
            m = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise UsageError(f"cannot read manifest {path}: {type(exc).__name__}")
    pub = m.get("public", {})
    if not pub.get("include"):
        raise UsageError("manifest: [public].include is empty")
    for sect, keys in (("public", {"include", "optional", "exclude"}),
                       ("deny", {"paths"}), ("commit", {"author_name", "author_email", "message"})):
        extra = set(m.get(sect, {})) - keys
        if extra:
            raise UsageError(f"manifest: unknown [{sect}] key(s) {sorted(extra)}")
    return m


def plan_files(source, manifest):
    """The sorted relative paths to copy (symlinks included, so they get refused)."""
    source = Path(source)
    pub = manifest["public"]
    excl = [glob_re(g) for g in pub.get("exclude", [])]
    picked = set()
    for required, entries in ((True, pub["include"]), (False, pub.get("optional", []))):
        for entry in entries:
            base = source / entry
            if not (base.exists() or base.is_symlink()):
                if required:
                    raise UsageError(f"required path missing from source: {entry}")
                continue
            if base.is_dir() and not base.is_symlink():
                picked |= {p.relative_to(source).as_posix() for p in base.rglob("*")
                           if p.is_symlink() or p.is_file()}
            else:
                picked.add(entry)
    return sorted(r for r in picked if ".git" not in r.split("/")
                  and not any(rx.match(r) for rx in excl))


def stage(source, files, dest):
    for rel in files:
        src = Path(source) / rel
        dst = Path(dest) / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            dst.symlink_to(os.readlink(src))  # refused by verify; never followed
        else:
            shutil.copy2(src, dst)


def deny_hits(files, manifest):
    rx = [glob_re(g, ci=True) for g in manifest.get("deny", {}).get("paths", [])]
    return [sc.Hit(f, 0, "deny") for f in files if any(r.match(f) for r in rx)]


def _tree_rel(name, tree, pathmod=os.path):
    """*name* relative to *tree*, else ``<outside>`` (never a ``..`` path or a foreign location).

    *pathmod* swaps the path flavour so Windows semantics are testable off
    Windows; the CLI always runs with the default ``os.path``.
    """
    if not isinstance(name, str) or not name:
        return "<unknown>"
    if pathmod.isabs(name):
        try:
            rel = pathmod.relpath(name, tree)
        except ValueError:
            return "<outside>"  # no common root (e.g. paths on different Windows drives)
    else:
        rel = pathmod.normpath(name)
    return "<outside>" if rel == ".." or rel.startswith(".." + pathmod.sep) or pathmod.isabs(rel) else rel


def run_gitleaks(tree, binary):
    """``(hits, warning)``; findings are reduced to file:line + rule id."""
    exe = binary or shutil.which("gitleaks")
    if not exe:
        return [], "gitleaks not installed: skipped (scrub_check still ran)"
    report = Path(tempfile.mkdtemp(prefix="gl-")) / "r.json"
    try:
        rc = subprocess.run([exe, "detect", "--no-git", "--redact", "--no-banner",
                             "--exit-code", "1", "--report-format", "json",
                             "--report-path", str(report), "-s", str(tree)],
                            capture_output=True, stdin=subprocess.DEVNULL).returncode
        data = json.loads(report.read_text() or "[]") if report.exists() else []
    except (OSError, ValueError) as exc:
        return [sc.Hit("gitleaks", 0, f"error:{type(exc).__name__}")], None
    finally:
        shutil.rmtree(report.parent, ignore_errors=True)
    if rc not in (0, 1):
        return [sc.Hit("gitleaks", 0, f"error:rc{rc}")], None
    hits = []
    for f in data:
        rel = _tree_rel(f.get("File"), tree)
        hits.append(sc.Hit(rel, int(f.get("StartLine", 0)), "gitleaks:" + str(f.get("RuleID", "?"))))
    if rc == 1 and not hits:
        hits.append(sc.Hit("gitleaks", 0, "findings"))
    return hits, None


def verify(tree, files, manifest, args):
    """All hits for the staged *tree* (deny, symlink/scrub, gitleaks) + warnings."""
    hits = deny_hits(files, manifest)
    rules = [args.manifest, *args.rules]
    cfg = sc.build_config(words_file=args.words, rules_files=rules)
    scrub_hits, skipped = sc.scan_tree(tree, cfg, with_skipped=True)
    hits += scrub_hits
    warns = [f"{sc.path_tag(s)}: binary file not scanned" for s in skipped]
    if not args.no_gitleaks:
        gl, warn = run_gitleaks(tree, args.gitleaks)
        hits += gl
        warns += [warn] if warn else []
    return hits, warns, cfg["_words"]


def fresh_repo(tree, manifest, date):
    c = manifest.get("commit", {})
    name = c.get("author_name", "plangraph contributors")
    mail = c.get("author_email", "noreply@example.invalid")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=mail,
               GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=mail)
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)

    def git(*a):
        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                        "-c", "init.defaultBranch=main", *a], cwd=tree, env=env, check=True,
                       capture_output=True)
    git("init", "-q")
    git("add", "-A", "-f")
    git("commit", "-q", "-m", c.get("message", "Initial public history"))


def build(args):
    manifest = load_manifest(args.manifest)
    source = Path(args.source).resolve()
    out = Path(args.out).resolve() if args.out else None
    if not source.is_dir():
        raise UsageError(f"source is not a directory: {args.source}")
    if not args.check:
        if out is None:
            raise UsageError("--out is required unless --check")
        if out.exists() and (not out.is_dir() or any(out.iterdir())):
            raise UsageError(f"--out exists and is not empty: {args.out}")
        out.parent.mkdir(parents=True, exist_ok=True)
    files = plan_files(source, manifest)
    tmp = Path(tempfile.mkdtemp(prefix=".mp-", dir=None if args.check else out.parent))
    try:
        tree = tmp / "tree"
        tree.mkdir()
        stage(source, files, tree)
        hits, warns, words = verify(tree, files, manifest, args)
        for w in warns:
            print(f"warning: {w}", file=sys.stderr)
        if hits:
            sc.report(hits, words=words)
            print("make_public: REFUSED -- nothing written", file=sys.stderr)
            return 1
        if args.check:
            print(f"make_public: check ok ({len(files)} files, no hits)")
            return 0
        fresh_repo(tree, manifest, args.date)
        if out.exists():
            out.rmdir()
        shutil.move(str(tree), str(out))
        print(f"make_public: built {out} ({len(files)} files, 1 commit)")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--source", required=True)
    ap.add_argument("--out")
    ap.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    ap.add_argument("--rules", action="append", default=[])
    ap.add_argument("--words")
    ap.add_argument("--gitleaks", help="path of the gitleaks binary (default: PATH lookup)")
    ap.add_argument("--no-gitleaks", action="store_true")
    ap.add_argument("--date", help="fixed commit date (ISO 8601) for reproducible builds")
    ap.add_argument("--check", action="store_true", help="build in a temp dir, report, delete")
    args = ap.parse_args(argv)
    try:
        return build(args)
    except UsageError as exc:
        print(f"make_public: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"make_public: error: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
