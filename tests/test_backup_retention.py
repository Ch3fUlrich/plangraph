"""Backup retention: pure planner, name allow/deny, prune I/O, CLI, config, hook.

Hermetic (D-852): tmp_path only, fake mtimes via os.utime, fixed ``now``; no real
data dir, no live keeper (the hook body is called directly with a stop event).
"""
import inspect
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from plangraph.keeper import backup_retention as br
from plangraph.keeper import cli, config, daemon

DAY = 86400


def ts(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()


NOW = ts(2026, 10, 15, 12)


def names(entries):
    return [e[0] for e in entries]


def ent(name, mtime, size=10):
    return (name, mtime, size)


def make(tmp_path, name, mtime, size=10):
    path = tmp_path / name
    path.write_bytes(b"x" * size)
    os.utime(path, (mtime, mtime))
    return path


# ------------------------------------------------------------------ planner
def test_keep_newest_n():
    es = [ent(f"pre-{i}.db", NOW - i * 3600) for i in range(6)]
    p = br.plan(es, keep=3, daily_days=0, now=NOW)
    assert names(p.keep) == ["pre-0.db", "pre-1.db", "pre-2.db"]
    assert names(p.delete) == ["pre-3.db", "pre-4.db", "pre-5.db"]
    assert p.bytes_to_free == 30


def test_keep_zero_and_daily_zero_deletes_all_past():
    es = [ent("pre-a.db", NOW - 5), ent("pre-b.db", NOW - 10)]
    p = br.plan(es, keep=0, daily_days=0, now=NOW)
    assert p.keep == [] and names(p.delete) == ["pre-a.db", "pre-b.db"]


def test_daily_bucket_per_utc_day():
    # three files today, two yesterday, one 5 days ago, one 30 days ago
    es = [ent("pre-t1.db", ts(2026, 10, 15, 1)), ent("pre-t2.db", ts(2026, 10, 15, 9)),
          ent("pre-t3.db", ts(2026, 10, 15, 11)),
          ent("pre-y1.db", ts(2026, 10, 14, 3)), ent("pre-y2.db", ts(2026, 10, 14, 20)),
          ent("pre-d5.db", ts(2026, 10, 10, 8)), ent("pre-old.db", ts(2026, 9, 15, 8))]
    p = br.plan(es, keep=0, daily_days=7, now=NOW)
    assert sorted(names(p.keep)) == ["pre-d5.db", "pre-t3.db", "pre-y2.db"]
    assert "pre-old.db" in names(p.delete)


def test_daily_window_edges():
    # daily_days=2 -> today and yesterday only
    es = [ent("pre-today.db", ts(2026, 10, 15, 0)), ent("pre-yest.db", ts(2026, 10, 14, 0)),
          ent("pre-two.db", ts(2026, 10, 13, 23, 59, 59))]
    p = br.plan(es, keep=0, daily_days=2, now=NOW)
    assert sorted(names(p.keep)) == ["pre-today.db", "pre-yest.db"]
    assert names(p.delete) == ["pre-two.db"]


def test_utc_midnight_boundary():
    late = ent("pre-late.db", ts(2026, 10, 14, 23, 59, 59))
    early = ent("pre-early.db", ts(2026, 10, 15, 0, 0, 0))
    p = br.plan([late, early], keep=0, daily_days=2, now=NOW)
    # different UTC days -> both are the newest of their own day
    assert sorted(names(p.keep)) == ["pre-early.db", "pre-late.db"]
    p1 = br.plan([late, early], keep=0, daily_days=1, now=NOW)
    assert names(p1.keep) == ["pre-early.db"] and names(p1.delete) == ["pre-late.db"]


def test_future_mtime_kept_and_not_counted():
    es = [ent("pre-future.db", NOW + 10 * DAY), ent("pre-a.db", NOW - 10),
          ent("pre-b.db", NOW - 20)]
    p = br.plan(es, keep=1, daily_days=0, now=NOW)
    assert "pre-future.db" in names(p.keep)
    assert "pre-a.db" in names(p.keep) and names(p.delete) == ["pre-b.db"]


def test_tie_break_by_name_is_deterministic():
    es = [ent("pre-c.db", NOW - 5), ent("pre-a.db", NOW - 5), ent("pre-b.db", NOW - 5)]
    first = br.plan(es, keep=1, daily_days=0, now=NOW)
    again = br.plan(list(reversed(es)), keep=1, daily_days=0, now=NOW)
    assert names(first.keep) == names(again.keep) == ["pre-a.db"]
    assert names(first.delete) == names(again.delete) == ["pre-b.db", "pre-c.db"]


@pytest.mark.parametrize("keep,days", [(-1, 0), (0, -1)])
def test_negative_args_rejected(keep, days):
    with pytest.raises(ValueError):
        br.plan([], keep=keep, daily_days=days, now=NOW)


def test_plan_does_not_mutate_or_read_clock():
    es = [ent("pre-a.db", NOW - 1)]
    snapshot = list(es)
    br.plan(es, 1, 1, NOW)
    assert es == snapshot


# -------------------------------------------------------------------- names
@pytest.mark.parametrize("name", [
    "backup-pre-m2b.db", "keeper.db.pre-restore", "keeper.db.bak-20260101",
    "pre-import.db", "keeper-pre-upgrade.db", "backup-pre-a.b_c-d.db"])
def test_backup_names_match(name):
    assert br.is_backup_name(name)


@pytest.mark.parametrize("name", [
    "keeper.db", "keeper.db-wal", "keeper.db-shm", "keeper.json",
    "keeper.json.pre-x", "keeper.json.bak", "a.token", "pre-x.token",
    "plan.snapshot.json", "plan.snapshot.json.sig", "md-sync.state", "md-sync-pre-x.db",
    "keeper.db.pre-x.lock", "keeper.db.bak-x.pid", "keeper.db.pre-x.alive",
    "keeper.db.pre-x.log", "backup-pre-x.db-wal", "pre-x.db-shm",
    "", ".", "..", "sub/pre-x.db", "pre-.db", "random.db", "backup-pre-x.db.old"])
def test_non_backup_names_never_match(name):
    assert not br.is_backup_name(name)


def test_deny_list_wins_over_allow_list():
    # would match the allow-list, but a deny rule applies
    assert br.is_denied("keeper.db.pre-x.token")
    assert not br.is_backup_name("keeper.db.pre-x.token")
    assert not br.is_backup_name("keeper.db.bak-x-wal")


# ------------------------------------------------------------------ scan/I/O
def test_scan_skips_symlinks_subdirs_and_non_backups(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "precious.bin"
    target.write_bytes(b"keep me")
    data = tmp_path / "data"
    data.mkdir()
    make(data, "pre-a.db", NOW - 100)
    make(data, "keeper.db", NOW - 100)
    (data / "backups").mkdir()
    make(data / "backups", "pre-inner.db", NOW - 100)
    (data / "pre-link.db").symlink_to(target)
    (data / "pre-dir.db").mkdir()
    es = br.scan(data)
    assert names(es) == ["pre-a.db"]
    res = br.prune(data, keep=0, daily_days=0, apply=True, now=NOW, out=lambda s: None)
    assert res.ok and res.deleted == ["pre-a.db"]
    assert target.read_bytes() == b"keep me"
    assert (data / "pre-link.db").is_symlink()
    assert (data / "backups" / "pre-inner.db").exists()
    assert (data / "pre-dir.db").is_dir()
    assert (data / "keeper.db").exists()


def test_deny_list_files_never_deleted(tmp_path):
    keepers = ["keeper.db", "keeper.db-wal", "keeper.db-shm", "keeper.json",
               "keeper.json.pre-x", "a.token", "plan.snapshot.json"]
    for n in keepers:
        make(tmp_path, n, NOW - 100 * DAY)
    make(tmp_path, "pre-old.db", NOW - 100 * DAY)
    res = br.prune(tmp_path, keep=0, daily_days=0, apply=True, now=NOW, out=lambda s: None)
    assert res.deleted == ["pre-old.db"]
    assert all((tmp_path / n).exists() for n in keepers)
    assert not any(n in names(res.plan.delete) for n in keepers)


def test_dry_run_default_deletes_nothing(tmp_path):
    for i in range(4):
        make(tmp_path, f"pre-{i}.db", NOW - i * 100, size=100)
    lines = []
    res = br.prune(tmp_path, keep=1, daily_days=0, now=NOW, out=lines.append)
    assert not res.applied
    assert len(list(tmp_path.iterdir())) == 4
    text = "\n".join(lines)
    assert "DRY RUN (no files deleted; pass --apply)" in text
    assert "du before: 400 bytes" in text
    assert "du after: 100 bytes (projected)" in text
    assert "bytes to free 300" in text
    assert "delete pre-1.db (100 bytes)" in text
    assert (res.du_before, res.du_after) == (400, 100)


def test_apply_deletes_exactly_the_plan(tmp_path):
    for i in range(5):
        make(tmp_path, f"pre-{i}.db", NOW - i * 100, size=50)
    make(tmp_path, "keeper.db", NOW - 9999, size=7)
    lines = []
    res = br.prune(tmp_path, keep=2, daily_days=0, apply=True, now=NOW, out=lines.append)
    assert sorted(res.deleted) == names(res.plan.delete) == ["pre-2.db", "pre-3.db", "pre-4.db"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keeper.db", "pre-0.db", "pre-1.db"]
    assert (res.du_before, res.du_after) == (257, 107)
    assert not any("projected" in ln for ln in lines)


def test_apply_reports_failures_and_counts(tmp_path, monkeypatch):
    make(tmp_path, "pre-a.db", NOW - 10)
    make(tmp_path, "pre-b.db", NOW - 20)

    real = os.unlink

    def flaky(path, *a, **k):
        if str(path).endswith("pre-b.db"):
            raise PermissionError("denied")
        return real(path, *a, **k)

    monkeypatch.setattr(br.os, "unlink", flaky)
    res = br.prune(tmp_path, keep=0, daily_days=0, apply=True, now=NOW, out=lambda s: None)
    assert not res.ok and res.deleted == ["pre-a.db"]
    assert res.failed == [("pre-b.db", "denied")]


def test_apply_rechecks_swapped_in_symlink(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("data")
    make(tmp_path, "pre-a.db", NOW - 10)
    pl = br.plan(br.scan(tmp_path), 0, 0, NOW)
    (tmp_path / "pre-a.db").unlink()
    (tmp_path / "pre-a.db").symlink_to(victim)       # swapped after planning
    assert names(pl.delete) == ["pre-a.db"]
    assert br._unlink_checked(str(tmp_path), "pre-a.db") == "not a regular file"
    assert victim.exists()


@pytest.mark.parametrize("bad", ["", "/", None])
def test_refuses_bad_dir(bad):
    with pytest.raises(ValueError):
        br.prune(bad, 1, 1)
    with pytest.raises(ValueError):
        br.scan(bad)


def test_refuses_missing_dir(tmp_path):
    with pytest.raises(ValueError):
        br.prune(tmp_path / "nope", 1, 1)


# --------------------------------------------------------------- CLI + guard
def run_cli(monkeypatch, argv):
    monkeypatch.delenv("PG_CONFIG", raising=False)
    return cli.main(argv)


def test_cli_dry_run_then_apply_without_config(tmp_path, monkeypatch, capsys):
    data = tmp_path / "d"
    data.mkdir()
    old = NOW - 400 * DAY
    for i in range(3):
        make(data, f"pre-{i}.db", old - i)
    assert run_cli(monkeypatch, ["admin", "prune-backups", "--dir", str(data),
                                 "--keep", "1", "--daily-days", "0"]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert len(list(data.iterdir())) == 3
    assert run_cli(monkeypatch, ["admin", "prune-backups", "--dir", str(data),
                                 "--keep", "1", "--daily-days", "0", "--apply"]) == 0
    assert [p.name for p in data.iterdir()] == ["pre-0.db"]


def test_cli_missing_dir_arg_exits_2(monkeypatch):
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["admin", "prune-backups"])
    assert exc.value.code == 2


def test_cli_bad_dir_and_bad_keep_exit_2(tmp_path, monkeypatch, capsys):
    assert run_cli(monkeypatch, ["admin", "prune-backups", "--dir", str(tmp_path / "x")]) == 2
    assert run_cli(monkeypatch, ["admin", "prune-backups", "--dir", "/"]) == 2
    assert run_cli(monkeypatch, ["admin", "prune-backups", "--dir", str(tmp_path),
                                 "--keep", "-1"]) == 2
    capsys.readouterr()


def test_cli_failed_delete_exits_1(tmp_path, monkeypatch, capsys):
    make(tmp_path, "pre-a.db", NOW - 400 * DAY)

    def boom(path, *a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(br.os, "unlink", boom)
    assert run_cli(monkeypatch, ["admin", "prune-backups", "--dir", str(tmp_path),
                                 "--keep", "0", "--daily-days", "0", "--apply"]) == 1
    capsys.readouterr()


def test_guard_no_default_dir():
    for fn in (br.prune, br.scan):
        param = inspect.signature(fn).parameters["dir"]
        assert param.default is inspect.Parameter.empty, fn.__name__
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if a.dest == "command").choices["admin"]
    prune = next(a for a in sub._actions if a.dest == "admin_command").choices["prune-backups"]
    dir_action = next(a for a in prune._actions if "--dir" in a.option_strings)
    assert dir_action.required is True and dir_action.default is None


def test_guard_module_source_has_no_data_path():
    src = Path(br.__file__).read_text(encoding="utf-8")
    for needle in ("." + "local/share", "~" + "/", "expand" + "user", "XDG_",
                   "environ", "Path.home", "gethome"):
        assert needle not in src, needle


# ------------------------------------------------------------------- config
def write_cfg(tmp_path, extra):
    path = tmp_path / "keeper.json"
    path.write_text(json.dumps(extra), encoding="utf-8")
    os.chmod(path, 0o600)
    return config.load_config(path)


def test_config_default_disabled(tmp_path):
    cfg = write_cfg(tmp_path, {})
    assert cfg.backup_prune["enabled"] is False and cfg.backup_prune["dir"] is None
    assert cfg.backup_prune["interval_s"] == 21600


def test_config_enabled_resolves_dir(tmp_path):
    cfg = write_cfg(tmp_path, {"backup_prune": {"enabled": True, "dir": "bk", "keep": 3,
                                                "daily_days": 2, "interval_s": 60}})
    assert cfg.backup_prune == {"enabled": True, "dir": str(tmp_path.resolve() / "bk"),
                                "keep": 3, "daily_days": 2, "interval_s": 60}


@pytest.mark.parametrize("bp", [
    {"enabled": True}, {"enabled": "yes"}, {"keep": -1}, {"interval_s": 0},
    {"daily_days": 1.5}, {"dir": ""}, "on"])
def test_config_bad_backup_prune(tmp_path, bp):
    with pytest.raises(config.ConfigError):
        write_cfg(tmp_path, {"backup_prune": bp})


# --------------------------------------------------------------------- hook
def test_hook_prunes_and_stops(tmp_path):
    for i in range(4):
        make(tmp_path, f"pre-{i}.db", NOW - 400 * DAY - i)
    stop = threading.Event()
    logs = []
    settings = {"dir": str(tmp_path), "keep": 1, "daily_days": 0, "interval_s": 3600}
    t = threading.Thread(target=daemon._backup_prune_loop, args=(settings, stop, logs.append))
    t.start()
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive()
    assert [p.name for p in tmp_path.iterdir()] == ["pre-0.db"]
    assert logs and "deleted 3" in logs[0]


def test_hook_logs_errors_never_raises(tmp_path):
    stop = threading.Event()
    logs = []
    stop.set()
    daemon._backup_prune_loop({"dir": str(tmp_path / "missing"), "keep": 1,
                               "daily_days": 0, "interval_s": 1}, stop, logs.append)
    assert logs == []                      # stop already set: loop body skipped

    stop2 = threading.Event()
    threading.Timer(0.05, stop2.set).start()
    daemon._backup_prune_loop({"dir": str(tmp_path / "missing"), "keep": 1,
                               "daily_days": 0, "interval_s": 30}, stop2, logs.append)
    assert logs and logs[0].startswith("backup_prune: error")
