import os
import json
import platform
import subprocess
import time

import pytest

from flint.core.vmm.cloud_hypervisor.jail import CloudHypervisorJail
from flint.core.vmm.firecracker.jail import FirecrackerJail



# Host-local: inspects one daemon's jails (skipped against a cluster control plane).
pytestmark = [
    pytest.mark.skipif(platform.system() != "Linux", reason="Linux backend only"),
    pytest.mark.usefixtures("daemon_only"),
]


def _jail(sandbox, backend_kind) -> tuple[str, str]:
    """(chroot base, chroot root) of the sandbox's VMM jail."""
    jail_type = CloudHypervisorJail if backend_kind == "linux-cloud-hypervisor" else FirecrackerJail
    jail = jail_type(sandbox.id)
    return jail.base, jail.root


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

    from .conftest import TEST_STATE_DIR
    with open(os.path.join(TEST_STATE_DIR, "state.json")) as stream:
        daemon = json.load(stream)
    api_uid, api_pid = daemon["api_uid"], daemon["api_pid"]
    assert api_uid >= 1_000_000 and api_uid != int(status["Uid"].split()[0])
    with open(f"/proc/{api_pid}/status") as stream:
        frontend = dict(line.split(":", 1) for line in stream if ":" in line)
    assert set(frontend["Uid"].split()) == {str(api_uid)}
    assert not frontend["Groups"].strip()
    assert int(frontend["CapEff"], 16) == int(frontend["CapPrm"], 16) == int(frontend["CapBnd"], 16) == 0
    assert frontend["NoNewPrivs"].strip() == "1"
    assert not os.path.exists(f"/proc/{api_pid}/root{TEST_STATE_DIR}/secret.key")
    assert not os.path.exists(f"/proc/{api_pid}/root{TEST_STATE_DIR}/r2nfs.sock")

    # Exercise the real worker socket as the API's identity. It must reject
    # command execution and extra fields trying to turn a guest connection
    # into a connection to a host service.
    probe = r'''
import json,socket,struct,sys
for operation,arguments in [
 ("exec",{"cmd":["id"]}),
 ("vm.connect",{"sandbox_id":sys.argv[2],"address":"127.0.0.1","port":22}),
 ("vm.create",{"template_id":"../../etc","tenant":"default"})]:
 s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); s.settimeout(5); s.connect(sys.argv[1])
 data=json.dumps({"operation":operation,"arguments":arguments,"actor":["",""]}).encode()
 s.sendall(struct.pack("!I",len(data))+data)
 n=struct.unpack("!I",s.recv(4,socket.MSG_WAITALL))[0]
 result=json.loads(s.recv(n,socket.MSG_WAITALL)); s.close()
 assert result["error"]["status"]==400,result
print("runtime rejects host commands, arbitrary connections and path traversal")
'''
    checked = subprocess.run(["setpriv", f"--reuid={api_uid}", f"--regid={api_uid}", "--clear-groups",
                              "/usr/bin/python3", "-c", probe, os.path.join(TEST_STATE_DIR, "runtime.sock"), sandbox.id],
                             capture_output=True, text=True, timeout=20)
    assert checked.returncode == 0, checked.stdout + checked.stderr

    # These are live kernel limits on the VMM, not configuration promises.
    from pathlib import Path
    from flint.core import config
    from flint.core._limits import DISK_BPS
    if backend_kind == "linux-cloud-hypervisor":
        group = Path(CloudHypervisorJail(sandbox.id).cgroup)
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
