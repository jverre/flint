"""Real bind-mount lifecycle checks; NFS coherency needs the KVM fleet rehearsal."""

import os
import subprocess

import pytest

from flint.core._shared_rootfs import SharedRootfs

pytestmark = pytest.mark.skipif(
    os.environ.get("FLINT_PRIVILEGED_TESTS") != "1",
    reason="requires isolated privileged Linux container",
)


def test_two_jails_see_one_disk_and_detach_preserves_data(tmp_path):
    # Use the actual disk implementation with a local test pool. Production
    # construction separately rejects local filesystems; this test isolates
    # file identity, mount and teardown semantics from NFS availability.
    pool = SharedRootfs.__new__(SharedRootfs)
    pool.directory = tmp_path / "pool"
    pool.directory.mkdir()
    pool.pool_id = "ec01ae4c-24d2-4cf5-842b-e53f13352074"
    template = tmp_path / "template"
    template.write_bytes(b"initial" + bytes(4096))
    vm = "07ce885b-bf8a-41c3-a9c5-6dc9f09cba11"
    pool.create(vm, str(template), os.getuid())
    first, second = tmp_path / "jail-a", tmp_path / "jail-b"
    try:
        pool.attach(vm, str(first))
        pool.attach(vm, str(second))
        assert os.stat(first).st_ino == os.stat(second).st_ino
        with first.open("r+b", buffering=0) as stream:
            stream.write(b"patched")
            os.fsync(stream.fileno())
        assert second.read_bytes().startswith(b"patched")
        subprocess.run(["umount", str(first)], check=True)
        first.unlink()
        assert second.read_bytes().startswith(b"patched")
        assert (pool.directory / vm / "rootfs.ext4").read_bytes().startswith(b"patched")
    finally:
        for target in (first, second):
            subprocess.run(["umount", str(target)], capture_output=True)
