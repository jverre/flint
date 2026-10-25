"""Resolve the kernel device identifier used by block IO ceilings."""
import os

import pytest

from flint.core._limits import _whole_device


def test_filesystem_devices_resolve_to_whole_disks(tmp_path):
    disk = tmp_path / "devices" / "nvme0n1"
    partition = disk / "nvme0n1p1"
    partition.mkdir(parents=True)
    (disk / "dev").write_text("259:0\n")
    (partition / "dev").write_text("259:1\n")
    (partition / "partition").write_text("1\n")
    index = tmp_path / "dev" / "block"
    index.mkdir(parents=True)
    (index / "259:0").symlink_to(disk, target_is_directory=True)
    (index / "259:1").symlink_to(partition, target_is_directory=True)
    assert _whole_device(os.makedev(259, 0), index) == "259:0"
    assert _whole_device(os.makedev(259, 1), index) == "259:0"
    with pytest.raises(RuntimeError, match="block-backed"):
        _whole_device(os.makedev(0, 42), index)
    with pytest.raises(FileNotFoundError):
        _whole_device(os.makedev(259, 99), index)
