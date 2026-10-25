import os
import platform
import time

import pytest

from flint.core._ch_jail import ChJail
from flint.core._jailer import JailSpec



# Host-local: inspects one daemon's jails (skipped against a cluster control plane).
pytestmark = [
    pytest.mark.skipif(platform.system() != "Linux", reason="Linux backend only"),
    pytest.mark.usefixtures("daemon_only"),
]


def _jail(sandbox, backend_kind) -> tuple[str, str]:
    """(chroot base, chroot root) of the sandbox's VMM jail."""
    if backend_kind == "linux-cloud-hypervisor":
        jail = ChJail(sandbox.id)
        return jail.base, jail.root
    spec = JailSpec(vm_id=sandbox.id, ns_name="")
    return spec.chroot_base, spec.chroot_root


def test_jailer_chroot_cleanup(sandbox, backend_kind):
    chroot_base, _ = _jail(sandbox, backend_kind)

    assert os.path.isdir(chroot_base), "Chroot should exist while VM is running"
    sandbox.kill()
    deadline = time.monotonic() + 5
    while os.path.exists(chroot_base) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not os.path.exists(chroot_base), "Chroot should be fully removed after kill"


def test_jailer_chroot_structure(sandbox, backend_kind):
    _, chroot_root = _jail(sandbox, backend_kind)

    assert os.path.isfile(f"{chroot_root}/rootfs.ext4"), "rootfs should be staged in chroot"
    sock = "ch.sock" if backend_kind == "linux-cloud-hypervisor" else "firecracker.sock"
    assert os.path.exists(f"{chroot_root}/{sock}"), "API socket should exist"


def test_vmm_runs_unprivileged_in_its_jail(sandbox, backend_kind):
    """The hypervisor process is chrooted, non-root, capability-less and can't regain privileges."""
    _, chroot_root = _jail(sandbox, backend_kind)
    pid = sandbox.pid
    with open(f"/proc/{pid}/status") as f:
        status = dict(line.split(":", 1) for line in f.read().splitlines() if ":" in line)

    # Compare the directory itself: FC's jailer pivot_roots in its own mount
    # namespace, so the link text reads "/" from the host.
    vmm_root, jail_root = os.stat(f"/proc/{pid}/root"), os.stat(chroot_root)
    assert (vmm_root.st_dev, vmm_root.st_ino) == (jail_root.st_dev, jail_root.st_ino), "VMM is not confined to its jail"
    assert all(uid != "0" for uid in status["Uid"].split()), status["Uid"]
    assert all(gid != "0" for gid in status["Gid"].split()), status["Gid"]
    assert "0" not in status["Groups"].split(), status["Groups"]
    assert int(status["CapEff"], 16) == 0 and int(status["CapPrm"], 16) == 0, (status["CapEff"], status["CapPrm"])
    assert status["NoNewPrivs"].strip() == "1"

    # These are live kernel limits on the VMM, not configuration promises.
    from pathlib import Path
    from flint.core import config
    from flint.core._limits import DISK_BPS
    if backend_kind == "linux-cloud-hypervisor":
        group = Path(ChJail(sandbox.id).cgroup)
    else:
        group = Path("/sys/fs/cgroup") / config.JAILER_PARENT_CGROUP / sandbox.id
    from flint.core._limits import verify_process
    verify_process(str(group), pid)
    assert "max" not in (group / "cpu.max").read_text()
    assert "max" not in (group / "memory.max").read_text()
    limits = (group / "io.max").read_text()
    assert f"rbps={DISK_BPS}" in limits and f"wbps={DISK_BPS}" in limits, limits


    # Restored VMMs must be placed and constrained just like a fresh boot.
    sandbox.pause()
    sandbox.resume()
    verify_process(str(group), sandbox.pid)
    assert "max" not in (group / "memory.max").read_text()
