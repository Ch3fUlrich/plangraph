"""Portable record metadata checks through the real poster, with mocked HTTP."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_seat_verdict_record import record_dir, seat_record, sv


@pytest.fixture
def posting(tmp_path, monkeypatch):
    argv = record_dir(tmp_path, "run-" + "1" * 32)
    argv.remove("--dry-run")
    argv.extend(["--url", "http://keeper.invalid"])
    sent = []

    def send(url, token, payload):
        assert url == "http://keeper.invalid" and token == "dummy"
        assert payload["key"] == "demo:A" and payload["ok"] is True
        sent.append(payload)
        return 200, "ok"

    monkeypatch.setattr(sv, "send", send)
    return argv, tmp_path / ("run-" + "1" * 32), sent


@pytest.mark.parametrize("has_getuid", [False, True])
def test_windows_record_skips_posix_owner_and_mode(posting, monkeypatch, has_getuid):
    argv, seat, sent = posting
    (seat / "record.json").chmod(0o777)

    def forbidden():
        pytest.fail("Windows must not call getuid")

    platform = SimpleNamespace(name="nt", **({"getuid": forbidden} if has_getuid else {}))
    monkeypatch.setattr(seat_record, "os", platform)
    assert sv.main(argv) == 0
    assert len(sent) == 1


def test_drive_mount_still_requires_linux_owner(posting, monkeypatch, capsys):
    argv, seat, sent = posting
    uid = (seat / "record.json").stat().st_uid
    # Compare the same foreign owner on Windows and POSIX, including drive mounts.
    monkeypatch.setattr(seat_record, "os", SimpleNamespace(name="nt", getuid=lambda: uid + 1))
    assert sv.main(argv) == 0
    capsys.readouterr()
    monkeypatch.setattr(seat_record, "os", SimpleNamespace(name="posix", getuid=lambda: uid + 1))
    monkeypatch.setattr(Path, "absolute", lambda self: Path("/mnt/c") / seat.name / self.name)
    assert sv.main(argv) == sv.EXIT_REFUSED
    assert "owned by the current uid" in capsys.readouterr().err
    assert len(sent) == 1


@pytest.mark.parametrize("mode", [0o620, 0o602], ids=["group", "other"])
@pytest.mark.parametrize("root", ["/native", "/mnt/data"])
def test_posix_record_group_or_other_writable_refused(posting, monkeypatch, capsys, mode, root):
    argv, seat, sent = posting
    path = seat / "record.json"
    path.chmod(mode)
    uid = path.stat().st_uid
    monkeypatch.setattr(seat_record, "os", SimpleNamespace(name="nt"))
    assert sv.main(argv) == 0
    capsys.readouterr()
    monkeypatch.setattr(seat_record, "os", SimpleNamespace(name="posix", getuid=lambda: uid))
    monkeypatch.setattr(Path, "absolute", lambda self: Path(root) / seat.name / self.name)
    assert sv.main(argv) == sv.EXIT_REFUSED
    assert "not group/world-writable" in capsys.readouterr().err
    assert len(sent) == 1


@pytest.mark.parametrize("drive", ["c", "Z"])
def test_native_record_link_into_drive_mount_keeps_mode_checks(
        posting, tmp_path, monkeypatch, capsys, drive):
    argv, seat, sent = posting
    path, target = seat / "record.json", tmp_path / "target.json"
    path.rename(target)
    target.chmod(0o666)
    path.symlink_to(target)
    uid = target.stat().st_uid
    monkeypatch.setattr(seat_record, "os", SimpleNamespace(name="posix", getuid=lambda: uid))
    mounted = Path("/mnt") / drive / seat.name / path.name
    monkeypatch.setattr(Path, "absolute", lambda self: mounted)
    monkeypatch.setattr(Path, "resolve", lambda self: mounted)
    # Synthetic drive modes are allowed only when the lexical path is on the drive.
    assert sv.main(argv) == 0
    capsys.readouterr()
    monkeypatch.setattr(Path, "absolute", lambda self: Path("/native") / seat.name / self.name)
    assert sv.main(argv) == sv.EXIT_REFUSED
    assert "not group/world-writable" in capsys.readouterr().err
    assert len(sent) == 1
