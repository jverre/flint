"""Host-side jail access must not follow links a hostile VMM planted."""

import os
import socket

import pytest

from flint.core._jailfs import connect_unix, copy_out, copy_within, own


@pytest.fixture
def victim(tmp_path):
    path = tmp_path / "host-secret"
    path.write_text("host secret")
    path.chmod(0o600)
    return path


def test_connect_unix_refuses_symlinked_socket(tmp_path):
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(tmp_path / "real.sock"))
    server.listen(1)
    os.symlink(tmp_path / "real.sock", tmp_path / "jail.sock")

    with pytest.raises(OSError):
        connect_unix(socket.socket(socket.AF_UNIX), str(tmp_path / "jail.sock"))

    client = socket.socket(socket.AF_UNIX)
    connect_unix(client, str(tmp_path / "real.sock"))
    client.close()
    server.close()


def test_copy_out_refuses_symlink_and_fifo(tmp_path, victim):
    os.symlink(victim, tmp_path / "memory-ranges")
    with pytest.raises(OSError):
        copy_out(str(tmp_path / "memory-ranges"), str(tmp_path / "out"))

    os.mkfifo(tmp_path / "fifo")  # must fail fast, not block the daemon
    with pytest.raises(OSError):
        copy_out(str(tmp_path / "fifo"), str(tmp_path / "out"))
    assert not (tmp_path / "out").exists()


def test_copy_out_keeps_holes(tmp_path):
    src = tmp_path / "mem"
    with open(src, "wb") as f:
        f.truncate(64 << 20)
        f.write(b"data")
    copy_out(str(src), str(tmp_path / "out"))
    assert (tmp_path / "out").read_bytes()[:4] == b"data"
    assert os.stat(tmp_path / "out").st_blocks * 512 < (1 << 20)


def test_copy_within_never_writes_through_planted_symlink(tmp_path, victim):
    (tmp_path / "mem").write_text("guest memory")
    os.symlink(victim, tmp_path / "pause-mem")

    copy_within(str(tmp_path / "mem"), str(tmp_path / "pause-mem"), os.getuid(), os.getgid())

    assert victim.read_text() == "host secret"
    assert not os.path.islink(tmp_path / "pause-mem")
    assert (tmp_path / "pause-mem").read_text() == "guest memory"


def test_own_refuses_symlink(tmp_path, victim):
    os.symlink(victim, tmp_path / "pause.tmp")
    with pytest.raises(OSError):
        own(str(tmp_path / "pause.tmp"), os.getuid(), os.getgid(), 0o777)
    assert victim.stat().st_mode & 0o777 == 0o600
