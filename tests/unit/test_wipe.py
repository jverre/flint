"""Wiping a sandbox's directory: its private files lose their data, nothing shared is touched."""

from __future__ import annotations

import os
import pytest

from flint.core import _wipe


def _write(path, data: bytes) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return str(path)


def test_private_files_are_wiped_before_removal(tmp_path, monkeypatch):
    disk = _write(tmp_path / "vm" / "rootfs.ext4", b"tenant secret " * 100_000)
    seen: dict[str, bytes] = {}
    real_rmtree = _wipe.shutil.rmtree

    def look_then_remove(path, **kw):
        with open(disk, "rb") as f:  # what is on disk just before removal
            seen["disk"] = f.read()
        real_rmtree(path, **kw)

    monkeypatch.setattr(_wipe.shutil, "rmtree", look_then_remove)
    _wipe.wipe_tree(str(tmp_path / "vm"), "vm-1")

    assert not os.path.exists(tmp_path / "vm")
    assert seen["disk"].count(0) == len(seen["disk"]), "data left in the file when it was removed"
    assert _wipe.take("vm-1")["files"] == 1
    assert _wipe.take("vm-1") == {"files": 0, "bytes": 0}

    # A failed durability check keeps the resource, so callers cannot report
    # successful deletion and lose the only path with which to retry it.
    retry = _write(tmp_path / "retry" / "rootfs.ext4", b"private" * 1000)
    real_sync = _wipe.os.fsync
    def unavailable(_fd):
        raise OSError("storage unavailable")
    monkeypatch.setattr(_wipe.os, "fsync", unavailable)
    with pytest.raises(_wipe.WipeError, match="retained for retry"):
        _wipe.wipe_tree(str(tmp_path / "retry"), "vm-retry")
    assert os.path.exists(retry)
    assert _wipe.take("vm-retry") == {"files": 0, "bytes": 0}
    monkeypatch.setattr(_wipe.os, "fsync", real_sync)
    monkeypatch.setattr(_wipe.shutil, "rmtree", real_rmtree)
    _wipe.wipe_tree(str(tmp_path / "retry"), "vm-retry")
    assert not os.path.exists(retry)


def test_shared_and_outside_files_are_never_touched(tmp_path):
    shared = _write(tmp_path / "shared" / "kernel", b"shared kernel")
    outside = _write(tmp_path / "host" / "passwd", b"host file")
    jail = tmp_path / "jail"
    os.makedirs(jail)
    os.link(shared, jail / "kernel")  # how read-only artifacts enter a jail
    os.symlink(outside, jail / "planted")  # what a jailed VMM could leave behind

    _wipe.wipe_tree(str(jail), "vm-2")

    assert not os.path.exists(jail)
    with open(shared, "rb") as f:
        assert f.read() == b"shared kernel"
    with open(outside, "rb") as f:
        assert f.read() == b"host file"
    assert _wipe.take("vm-2")["files"] == 0


def test_a_directory_swapped_for_a_symlink_mid_walk_is_not_followed(tmp_path, monkeypatch):
    """A live VMM owns its jail's files: while the wipe works through a
    directory, it can replace that directory with a symlink to elsewhere."""
    host = tmp_path / "host"
    for name in ("f1", "f2"):
        _write(host / name, b"host secret")
        _write(tmp_path / "jail" / "x" / name, b"vm data")
    real = _wipe._wipe_fd

    def swap_then_wipe(fd):
        x = tmp_path / "jail" / "x"
        if not x.is_symlink():  # after the first file, x points at the host
            os.rename(x, tmp_path / "jail" / "moved")
            os.symlink(host, x)
        return real(fd)

    monkeypatch.setattr(_wipe, "_wipe_fd", swap_then_wipe)
    _wipe.wipe_tree(str(tmp_path / "jail"), "vm-3")

    for name in ("f1", "f2"):
        with open(host / name, "rb") as f:
            assert f.read() == b"host secret", f"{name} outside the jail was wiped"
    _wipe.take("vm-3")
