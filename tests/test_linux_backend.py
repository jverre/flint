import os
import platform
import time

import pytest

from flint.core._jailer import JailSpec


pytestmark = pytest.mark.skipif(platform.system() != "Linux", reason="Linux backend only")


@pytest.fixture(autouse=True)
def _firecracker_only(backend_kind):
    # Only Firecracker runs under the jailer; Cloud Hypervisor has no chroot yet.
    if backend_kind != "linux-firecracker":
        pytest.skip(f"jailer layout is Firecracker-only (backend: {backend_kind})")


def test_jailer_chroot_cleanup(sandbox):
    vm_id = sandbox.id
    spec = JailSpec(vm_id=vm_id, ns_name=f"fc-{vm_id[:8]}")
    chroot_base = spec.chroot_base

    assert os.path.isdir(chroot_base), "Chroot should exist while VM is running"
    sandbox.kill()
    time.sleep(0.5)
    assert not os.path.exists(chroot_base), "Chroot should be fully removed after kill"


def test_jailer_chroot_structure(sandbox):
    vm_id = sandbox.id
    spec = JailSpec(vm_id=vm_id, ns_name=f"fc-{vm_id[:8]}")

    assert os.path.isfile(f"{spec.chroot_root}/rootfs.ext4"), "rootfs should be staged in chroot"
    assert os.path.exists(f"{spec.chroot_root}/firecracker.sock"), "API socket should exist"
