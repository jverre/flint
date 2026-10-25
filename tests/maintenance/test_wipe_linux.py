"""Trusted artifact ownership and wiping require a privileged Linux host."""
import os
import shutil
import subprocess

import pytest

from flint.core import _wipe

pytestmark = pytest.mark.skipif(
    os.environ.get("FLINT_PRIVILEGED_TESTS") != "1",
    reason="isolated privileged Linux container required",
)


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def test_shared_and_outside_files_are_never_touched(tmp_path, monkeypatch):
    shared = _write(tmp_path / "shared" / "kernel", b"shared kernel")
    outside = _write(tmp_path / "host" / "passwd", b"host file")
    jail = tmp_path / "jail"
    os.makedirs(jail)
    from flint.core import config
    from flint.core._artifacts import shared_readonly
    mirror = tmp_path / "mirrors"
    monkeypatch.setattr(config, "JAILER_SHARED_DIR", str(mirror))
    os.link(shared_readonly(shared, str(mirror)), jail / "kernel")  # how read-only artifacts enter a jail
    os.symlink(outside, jail / "planted")  # what a jailed VMM could leave behind

    # Another VM can still be running a shared VMM executable at deletion.
    binary = tmp_path / "shared" / "sleep"
    shutil.copy2(shutil.which("sleep"), binary)
    os.link(shared_readonly(str(binary), str(mirror), mode=0o555), jail / "vmm")
    process = subprocess.Popen([str(binary), "60"])
    try:
        _wipe.wipe_tree(str(jail), "vm-2")
        assert process.poll() is None
    finally:
        process.kill()
        process.wait(timeout=5)

    assert not os.path.exists(jail)
    with open(shared, "rb") as f:
        assert f.read() == b"shared kernel"
    with open(outside, "rb") as f:
        assert f.read() == b"host file"
    assert _wipe.take("vm-2")["files"] == 0

