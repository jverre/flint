"""Shared volumes: create them, mount them into running sandboxes, share files."""

from __future__ import annotations

import time
import uuid

import httpx
import pytest

from flint import Sandbox, Volume

from .conftest import TEST_PORT

BASE = f"http://127.0.0.1:{TEST_PORT}"


@pytest.fixture
def volume(_ensure_daemon):
    vol = Volume.create(f"test-{uuid.uuid4().hex[:8]}")
    yield vol
    vol.delete()


@pytest.fixture
def make_volume(_ensure_daemon):
    """Create volumes on demand; all are deleted after the test (after its sandboxes are killed)."""
    created: list[Volume] = []

    def make(create=lambda name: Volume.create(name)) -> Volume:
        vol = create(f"test-{uuid.uuid4().hex[:8]}")
        created.append(vol)
        return vol

    yield make
    for vol in reversed(created):
        vol.delete()


@pytest.fixture
def make_sandbox(require_sandbox):
    """Create sandboxes on demand; all are killed (releasing their mounts) after the test."""
    created: list[Sandbox] = []

    def make() -> Sandbox:
        sb = Sandbox()
        created.append(sb)
        return sb

    yield make
    for sb in created:
        sb.kill()


def sh(sb: Sandbox, cmd: str) -> str:
    result = sb.commands.run(cmd)
    assert result.exit_code == 0, f"{cmd!r} failed in {sb.id[:8]}: {result.stdout}{result.stderr}"
    return result.stdout


def wait_for(sb: Sandbox, cmd: str, timeout: float = 10) -> str:
    """Poll until cmd succeeds in the sandbox; return its output."""
    deadline = time.monotonic() + timeout
    while True:
        result = sb.commands.run(cmd)
        if result.exit_code == 0:
            return result.stdout
        if time.monotonic() > deadline:
            pytest.fail(f"{cmd!r} still failing after {timeout}s in {sb.id[:8]}: {result.stdout}{result.stderr}")
        time.sleep(0.2)


def mount_error(sb: Sandbox, vol: Volume, path: str, **kwargs) -> httpx.HTTPStatusError:
    with pytest.raises(httpx.HTTPStatusError) as exc:
        sb.mount(vol, path, **kwargs)
    return exc.value


def test_volume_crud(_ensure_daemon):
    name = f"test-{uuid.uuid4().hex[:8]}"
    vol = Volume.create(name)
    try:
        assert Volume.get(name) == vol
        assert vol in Volume.list()
        assert httpx.post(f"{BASE}/volumes", json={"name": name}).status_code == 409
        assert httpx.post(f"{BASE}/volumes", json={"name": "../escape"}).status_code == 400
    finally:
        vol.delete()
    assert Volume.get(name) is None
    assert httpx.delete(f"{BASE}/volumes/{vol.id}").status_code == 404


def test_children_write_parent_reads_live(volume, make_sandbox):
    """Each child writes its own subpath; the parent sees every child's output while they run."""
    parent = make_sandbox()
    parent.mount(volume, "/results", readonly=True)
    children = [make_sandbox() for _ in range(3)]
    for child in children:
        child.mount(volume, "/out", subpath=child.id)
        sh(child, f"echo 'done by {child.id}' > /out/result.txt")
        assert sh(child, "ls /out").split() == ["result.txt"], "a child sees only its own subpath"

    for child in children:
        out = wait_for(parent, f"cat /results/{child.id}/result.txt")
        assert out.strip() == f"done by {child.id}"
    # Root in the guest can remount read-write; the host must still refuse.
    denied = parent.commands.run("mount -o remount,rw /results && touch /results/nope")
    assert denied.exit_code != 0, f"read-only mount accepted a write: {denied.stdout}"
    assert {m["path"]: m["readonly"] for m in parent.mounts} == {"/results": True}


def test_one_writer_per_subtree(volume, make_sandbox):
    a, b = make_sandbox(), make_sandbox()
    a.mount(volume, "/w", subpath="shared")

    err = mount_error(b, volume, "/w", subpath="shared/inner")
    assert err.response.status_code == 409 and a.id in str(err)
    assert mount_error(b, volume, "/w").response.status_code == 409, "whole-volume writer overlaps"
    b.mount(volume, "/r", subpath="shared", readonly=True)
    b.mount(volume, "/elsewhere", subpath="other")

    a.unmount("/w")
    b.mount(volume, "/w", subpath="shared/inner")
    sh(b, "echo mine > /w/f")
    assert wait_for(b, "cat /r/inner/f").strip() == "mine"


def test_mount_survives_pause_resume_and_unmount(volume, make_sandbox):
    sb = make_sandbox()
    sb.mount(volume, "/data")
    sh(sb, "echo before > /data/f")
    sb.pause()
    sb.resume()
    assert sh(sb, "cat /data/f").strip() == "before"
    sh(sb, "echo after >> /data/f")

    sb.unmount("/data")
    assert " /data " not in sh(sb, "cat /proc/mounts")
    assert sb.mounts == []
    other = make_sandbox()
    other.mount(volume, "/data", readonly=True)
    assert sh(other, "cat /data/f").split() == ["before", "after"]


def test_kill_releases_mounts(volume, make_sandbox):
    a, b = make_sandbox(), make_sandbox()
    a.mount(volume, "/w", subpath="job")
    sh(a, "echo partial > /w/f")
    a.kill()
    b.mount(volume, "/w", subpath="job")
    assert sh(b, "cat /w/f").strip() == "partial"
    assert httpx.delete(f"{BASE}/volumes/{volume.id}").status_code == 409, "can't delete a mounted volume"


def test_32_mounts_in_one_sandbox(volume, make_sandbox):
    writer, reader = make_sandbox(), make_sandbox()
    writer.mount(volume, "/w")
    sh(writer, "for i in $(seq 32); do mkdir -p /w/c$i && echo $i > /w/c$i/v; done")
    for i in range(1, 33):
        reader.mount(volume, f"/m/c{i}", subpath=f"c{i}", readonly=True)
    out = sh(reader, "for i in $(seq 32); do cat /m/c$i/v; done")
    assert out.split() == [str(i) for i in range(1, 33)]


def test_mounts_are_private_to_their_sandbox(volume, make_sandbox):
    """Another sandbox can't use a mount's export, and a writer can't redirect a reader onto host files."""
    writer, reader, outsider = make_sandbox(), make_sandbox(), make_sandbox()
    writer.mount(volume, "/w")
    sh(writer, "mkdir -p /w/sub && echo marker > /w/sub/marker")
    m = reader.mount(volume, "/r", subpath="sub", readonly=True)

    stolen = outsider.commands.run(
        f"mkdir -p /steal && mount -t nfs -o vers=3,proto=tcp,port=2049,mountport=2049,mountproto=tcp,nolock "
        f"10.0.0.1:/{m['id']} /steal"
    )
    assert stolen.exit_code != 0, f"outsider mounted another sandbox's export: {stolen.stdout}"

    # Swap the reader's directory for a symlink to / on the host side.
    sh(writer, "mv /w/sub /w/sub.old && ln -s / /w/sub")
    assert sh(reader, "ls /r").split() == ["marker"]
    late = make_sandbox()
    late.mount(volume, "/r", subpath="sub", readonly=True)
    assert "etc" not in sh(late, "ls /r").split(), "symlinked subpath resolved outside the volume"


def test_snapshot_is_a_frozen_read_only_copy(make_volume, make_sandbox):
    """A snapshot taken while a sandbox is writing keeps the data as it was then, read-only."""
    vol = make_volume()
    assert vol.kind == "image", "this host should serve image volumes (is the nbd module available?)"
    writer, reader = make_sandbox(), make_sandbox()
    writer.mount(vol, "/w")
    sh(writer, "echo v1 > /w/state && sync")
    snap = make_volume(lambda name: vol.snapshot(name))
    sh(writer, "echo v2 > /w/state")

    assert snap.readonly and snap.kind == "image"
    reader.mount(snap, "/snap", readonly=True)
    assert sh(reader, "cat /snap/state").strip() == "v1"
    assert sh(writer, "cat /w/state").strip() == "v2"
    err = mount_error(reader, snap, "/rw")
    assert err.response.status_code == 400, "a snapshot can't be mounted read-write"


def test_fork_is_independent(make_volume, make_sandbox):
    vol = make_volume()
    a, b = make_sandbox(), make_sandbox()
    a.mount(vol, "/v")
    sh(a, "echo base > /v/f")
    fork = make_volume(lambda name: vol.fork(name))
    assert fork.kind == "image" and not fork.readonly
    b.mount(fork, "/v")
    sh(b, "echo fork >> /v/f")
    sh(a, "echo orig >> /v/f")
    assert sh(b, "cat /v/f").split() == ["base", "fork"]
    assert sh(a, "cat /v/f").split() == ["base", "orig"]


def test_volume_size_is_what_the_sandbox_sees(make_volume, make_sandbox):
    vol = make_volume(lambda name: Volume.create(name, size_gib=2))
    assert vol.size_gib == 2
    sb = make_sandbox()
    sb.mount(vol, "/v")
    total_kib = int(sh(sb, "df -P -k /v | tail -1").split()[1])
    assert 1.8 * 2**20 < total_kib <= 2 * 2**20, f"df reports {total_kib} KiB for a 2 GiB volume"
