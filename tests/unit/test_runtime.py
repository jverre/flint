"""The private runtime protocol bounds input and transfers capabilities as FDs."""
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import platform

import pytest

from flint.core._identities import identity, release, _subordinate_id
from flint.daemon.runtime_protocol import MAX_FRAME, receive, send
from flint.core._confinement import supervised_spawn


@pytest.mark.skipif(platform.system() != "Linux", reason="Linux parent-death semantics")
def test_confined_process_outlives_the_request_that_started_it():
    started = []
    def launch():
        process = subprocess.Popen([sys.executable, "-c",
            "import ctypes,signal,sys; ctypes.CDLL(None).prctl(1,signal.SIGTERM); "
            "print('ready',flush=True); sys.stdin.readline()"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert process.stdout.readline() == b"ready\n"
        return process
    request = threading.Thread(target=lambda: started.append(supervised_spawn(launch)))
    request.start()
    request.join(timeout=5)
    assert not request.is_alive() and started
    process = started[0]
    try:
        process.communicate(b"finished\n", timeout=5)
        assert process.returncode == 0, "request teardown killed its long-lived parser"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_workload_identities_are_distinct_and_survive_reopening(tmp_path):
    vm = identity("vm:first", directory=str(tmp_path))
    peer = identity("vm:second", directory=str(tmp_path))
    proxy = identity("proxy:first", directory=str(tmp_path))
    assert len({vm, peer, proxy}) == 3
    assert all(uid >= 1_000_000 and uid == gid for uid, gid in (vm, peer, proxy))
    assert identity("vm:first", directory=str(tmp_path)) == vm
    release("vm:first", directory=str(tmp_path))
    assert identity("vm:first", directory=str(tmp_path)) == vm


def test_identity_collision_never_shares_a_user(tmp_path):
    identity("vm:first", directory=str(tmp_path))
    with sqlite3.connect(tmp_path / "identities.sqlite") as db:
        db.execute("UPDATE identities SET owner = 'vm:other'")
    with pytest.raises(RuntimeError, match="collision"):
        identity("vm:first", directory=str(tmp_path))


def test_delegated_container_identities_cannot_be_used_for_host_workloads(tmp_path):
    ranges = tmp_path / "subuid"
    ranges.write_text("# rootless users\nalice:1000000:65536\nbob:2000000:65536\n")
    assert not _subordinate_id(999999, ranges)
    assert _subordinate_id(1000000, ranges)
    assert _subordinate_id(1065535, ranges)
    assert not _subordinate_id(1065536, ranges)
    assert _subordinate_id(2000000, ranges)
    ranges.write_text("alice:not-an-id:65536\n")
    with pytest.raises(RuntimeError, match="configuration"):
        _subordinate_id(1000000, ranges)


def test_runtime_transfers_an_open_connection_without_namespace_access():
    control, peer = socket.socketpair()
    guest, service = socket.socketpair()
    with control, peer, guest, service:
        send(control, {"value": None}, guest.fileno())
        result, fd = receive(peer)
        assert result == {"value": None}
        with socket.socket(fileno=fd) as connection:
            connection.sendall(b"guest request")
            assert service.recv(100) == b"guest request"
        # Closing the transferred capability does not close the worker's copy.
        guest.sendall(b"still open")
        assert service.recv(100) == b"still open"


@pytest.mark.parametrize("payload", [b"[]", b"null", b"{invalid"])
def test_runtime_requires_json_objects(payload):
    writer, reader = socket.socketpair()
    with writer, reader:
        writer.sendall(struct.pack("!I", len(payload)) + payload)
        with pytest.raises(ValueError):
            receive(reader)


@pytest.mark.parametrize("length", [0, MAX_FRAME + 1])
def test_runtime_rejects_oversized_frames_before_reading_the_body(length):
    writer, reader = socket.socketpair()
    with writer, reader:
        writer.sendall(struct.pack("!I", length))
        with pytest.raises(ValueError, match="limit"):
            receive(reader)
