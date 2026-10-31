"""tests/test_spawner_sessions_config.py — SESSION-SPAWN-MCP config trust/scope tests."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
import pytest
from plangraph.routing_errors import ParameterRefused
from sessions_support import UUID1, PROMPT, FakeRun, write_config, make, go
from sessions_support import site  # noqa: F401


def test_config_requires_existing_real_files(site):
    cfg = json.loads(site.cfg.read_text())
    bad = site.tmp_path / "sessions-bad.json"
    write_config(bad, json.dumps({**cfg, "mcp_configs": [str(site.tmp_path / "ghost-mcp.json")]}))
    s = make(site, FakeRun(), config_path=bad)
    with pytest.raises(ParameterRefused, match="mcp_configs entry is not a regular file"):
        go(site, s)  # every configured entry must resolve to an existing regular file
    gone = site.tmp_path / "gone.json"
    write_config(gone, "{}")
    cfg2 = site.tmp_path / "sessions-gone.json"
    write_config(cfg2, json.dumps({**cfg, "mcp_configs": [str(gone)]}))
    s2 = make(site, FakeRun(), config_path=cfg2)
    gone.unlink()
    with pytest.raises(ParameterRefused, match="mcp_config"):
        go(site, s2, mcp_config=str(gone))  # requested entry checked again at launch time
    cfg3 = site.tmp_path / "sessions-dir.json"
    write_config(cfg3, json.dumps({**cfg, "mcp_configs": [str(site.tmp_path)]}))
    with pytest.raises(ParameterRefused, match="mcp_configs entry is not a regular file"):
        go(site, make(site, FakeRun(), config_path=cfg3))  # a directory is not a regular file


def test_config_refuses_root_roots_and_bad_scalars(site):
    cfg = json.loads(site.cfg.read_text())

    def refused(over, match):
        bad = site.tmp_path / "sessions-bad.json"
        write_config(bad, json.dumps({**cfg, **over}))
        with pytest.raises(ParameterRefused, match=match):
            go(site, make(site, FakeRun(), config_path=bad))

    refused({"cwd_roots": ["/"]}, "must not be a filesystem root")  # a filesystem root would allow any cwd
    refused({"cwd_roots": [str(site.mcp)]}, "entry is not an existing directory")  # a file is not a directory
    refused({"cwd_roots": [str(site.tmp_path / "ghost-dir")]}, "entry is not an existing directory")  # must exist
    refused({"file_roots": ["/"]}, "unknown")  # removed keys are refused only as unknown keys
    refused({"style_file": "style.txt"}, "unknown")
    refused({"style_file": str(site.tmp_path / "ghost-style.txt")}, "unknown")
    refused({"compact_window": "33x"}, "compact_window must be a digit string")
    refused({"compact_window": 330000}, "compact_window must be a digit string")
    refused({"ctx_cap_tokens": 0}, "ctx_cap_tokens must be a positive int")
    refused({"ctx_cap_tokens": "300000"}, "ctx_cap_tokens must be a positive int")
    run = FakeRun()
    s = make(site, run)
    with pytest.raises(ParameterRefused, match="dry_run"):
        go(site, s, dry_run=1)  # dry_run must be a real bool
    assert not run.calls


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX config ownership model")
def test_config_trust_refuses_loose_permissions(site, monkeypatch):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    site.cfg.chmod(0o666)  # world-writable config: not ours to trust
    with pytest.raises(ParameterRefused, match="mode must be 0600 or stricter"):
        go(site, s)
    site.cfg.chmod(0o664)  # group-writable refused too
    with pytest.raises(ParameterRefused, match="mode must be 0600 or stricter"):
        go(site, s)
    site.cfg.chmod(0o600)
    go(site, s)  # owner-only config launches fine
    st = site.cfg.stat()
    monkeypatch.setattr(os, "getuid", lambda: st.st_uid + 1)  # foreign owner refused
    with pytest.raises(ParameterRefused, match="not owned by the current user"):
        go(site, s, name="beta")


def test_config_symlink_refused(site):
    alias = site.tmp_path / "alias-cfg2.json"
    alias.symlink_to(site.cfg)
    with pytest.raises(ParameterRefused, match="symlink"):
        go(site, make(site, FakeRun(), config_path=alias))  # O_NOFOLLOW refuses the alias


def test_config_mode_0640_refused(site):
    site.cfg.chmod(0o640)  # group-readable: any group/other bit refuses
    try:
        with pytest.raises(ParameterRefused, match="0600"):
            go(site, make(site, FakeRun()))
    finally:
        site.cfg.chmod(0o600)


def test_config_owned_by_other_uid_refused(site, monkeypatch):
    # A non-root test cannot chown the config, so falsify the owner its fd
    # reports to fstat: the check must notice the file is not ours.
    ino = os.stat(site.cfg).st_ino
    real_fstat = os.fstat

    def lying_fstat(fd, *args, **kwargs):
        st = real_fstat(fd, *args, **kwargs)
        if st.st_ino == ino:
            return os.stat_result((st[0], st[1], st[2], st[3], 12345, st[5], st[6], st[7], st[8], st[9]))
        return st

    monkeypatch.setattr(os, "fstat", lying_fstat)
    with pytest.raises(ParameterRefused, match="owned"):
        go(site, make(site, FakeRun()))


def test_config_read_without_no_follow_refused(site, monkeypatch):
    monkeypatch.delattr(os, "O_NOFOLLOW")  # no safe open possible: refuse, never fall back
    with pytest.raises(ParameterRefused, match="O_NOFOLLOW"):
        go(site, make(site, FakeRun()))


def test_config_style_text_non_string_refused(site):
    for bad in (5, None, "", ["be terse"]):
        bad_cfg = site.tmp_path / "sessions-style-bad.json"
        write_config(bad_cfg, json.dumps({**json.loads(site.cfg.read_text()), "style_text": bad}))
        with pytest.raises(ParameterRefused, match="style_text"):
            go(site, make(site, FakeRun(), config_path=bad_cfg))


def test_config_refuses_old_keys_as_unknown(site):
    for key, value in (("style_file", "style.txt"), ("file_roots", [str(site.tmp_path)]),
                       ("windows_launcher", "wt")):
        old_cfg = site.tmp_path / "sessions-old-key.json"
        write_config(old_cfg, json.dumps({**json.loads(site.cfg.read_text()), key: value}))
        with pytest.raises(ParameterRefused, match="unknown"):
            go(site, make(site, FakeRun(), config_path=old_cfg))


def test_config_refuses_symlink_and_nonregular(site):
    alias = site.tmp_path / "alias-cfg.json"
    alias.symlink_to(site.cfg)
    s = make(site, FakeRun(), config_path=alias)
    with pytest.raises(ParameterRefused, match="symlink"):
        go(site, s)  # the config path itself must be the real file
    dircfg = site.tmp_path / "cfg-dir"
    dircfg.mkdir()
    s2 = make(site, FakeRun(), config_path=dircfg)
    with pytest.raises(ParameterRefused, match="regular"):
        go(site, s2)


def test_config_owner_mismatch_refused(site, monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 12345)  # config owned by someone else
    with pytest.raises(ParameterRefused, match="owned"):
        go(site, make(site, FakeRun()))


def test_config_world_writable_refused(site):
    site.cfg.chmod(0o666)  # regular file, but anyone may rewrite it
    try:
        with pytest.raises(ParameterRefused, match="group/world-writable"):
            go(site, make(site, FakeRun()))
    finally:
        site.cfg.chmod(0o600)


def test_config_and_style_are_read_from_the_checked_fd(site, monkeypatch):
    import io
    cfg2 = site.tmp_path / "sessions-fd.json"
    write_config(cfg2, json.dumps({**json.loads(site.cfg.read_text()), "style_text": "be terse"}))
    forbidden = {str(cfg2.resolve())}
    real_open, real_io_open = open, io.open

    def guard(fn):
        def wrapped(file, *args, **kwargs):
            # A by-name open of the config is exactly the finding: the checked
            # fd is the only fd its bytes may come from (style_text rides in the
            # config itself now - the style_file half is a cut feature).
            if isinstance(file, (str, Path)) and str(Path(file).resolve()) in forbidden:
                raise AssertionError("the config must be read from the checked fd")
            return fn(file, *args, **kwargs)
        return wrapped

    monkeypatch.setattr("builtins.open", guard(real_open))
    monkeypatch.setattr(io, "open", guard(real_io_open))
    run = FakeRun(stdout="session " + UUID1)
    res = go(site, make(site, run, config_path=cfg2))
    assert res["ok"] is True
    argv, _ = run.calls[0]
    assert argv[-3:] == ["--append-system-prompt", "be terse", PROMPT]


def test_config_rejects_unknown_keys_and_bad_scalar_types(site):
    cfg = json.loads(site.cfg.read_text())

    def refused(over, match):
        bad = site.tmp_path / "sessions-bad.json"
        write_config(bad, json.dumps({**cfg, **over}))
        with pytest.raises(ParameterRefused, match=match):
            go(site, make(site, FakeRun(), config_path=bad))

    refused({"extra": 1}, "unknown")  # an unknown top-level key refuses every action
    refused({"style_file": None}, "unknown")  # removed keys are refused only as unknown keys
    refused({"style_file": 5}, "unknown")
    refused({"compact_window": None}, "compact_window must be a digit string")  # a present key must carry its type - null is not a value
    refused({"compact_window": []}, "compact_window must be a digit string")
    refused({"ctx_cap_tokens": None}, "ctx_cap_tokens must be a positive int")
    refused({"ctx_cap_tokens": True}, "ctx_cap_tokens must be a positive int")  # a bool is not a positive int
    refused({"ctx_cap_tokens": 1.5}, "ctx_cap_tokens must be a positive int")
    refused({"windows_launcher": None}, "unknown")  # the wt launcher is cut: the key is refused outright
    refused({"windows_launcher": "cmd"}, "unknown")
    refused({"windows_launcher": 1}, "unknown")
