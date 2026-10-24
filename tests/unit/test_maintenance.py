from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid

import pytest

from flint.cluster import maintenance
from flint.core import _shared_rootfs as disks
from flint.core._migration_network import MigrationNetwork

VM = "07ce885b-bf8a-41c3-a9c5-6dc9f09cba11"
POOL = "ec01ae4c-24d2-4cf5-842b-e53f13352074"


def test_inventory_does_not_block_lease_path(monkeypatch):
    blocked = threading.Event()
    started = threading.Event()

    def sample(*args):
        started.set()
        blocked.wait(2)
        return {"observed_at": 10}

    inventory = maintenance.PatchInventory()
    monkeypatch.setattr(inventory, "_sample", sample)
    start = time.monotonic()
    assert inventory.report("binary", {}) == {"status": "collecting"}
    assert time.monotonic() - start < 0.2
    assert started.wait(1)
    blocked.set()
    for _ in range(100):
        if inventory.report("binary", {}).get("observed_at") == 10:
            break
        time.sleep(0.01)
    assert inventory.report("binary", {})["observed_at"] == 10


@pytest.mark.skipif(
    __import__("platform").system() != "Linux",
    reason="process executable inventory uses Linux /proc",
)
def test_replacing_installed_binary_does_not_relabel_running_inode(tmp_path):
    old = tmp_path / "vmm-old"
    installed = tmp_path / "vmm"
    old.write_text("#!/bin/sh\necho old-vmm\n")
    old.chmod(0o755)
    os.link(old, installed)
    before = maintenance.binary_identity(str(old))
    replacement = tmp_path / "replacement"
    replacement.write_text("#!/bin/sh\necho new-vmm\n")
    replacement.chmod(0o755)
    replacement.replace(installed)
    assert maintenance.binary_identity(str(old)) == before
    assert maintenance.binary_identity(str(installed))["version"] == "new-vmm"
    assert before["sha256"] != maintenance.binary_identity(str(installed))["sha256"]


def test_missing_patch_tools_are_unknown(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", missing)
    assert maintenance.command_json(["canonical-livepatch"])["status"] == "unknown"


@pytest.fixture
def pool(tmp_path, monkeypatch):
    # Disk semantics use real files; mock only the mount classification and
    # ownership needed to run the unit suite without root or an NFS server.
    root = tmp_path / "pool"
    root.mkdir(mode=0o755)
    (root / "pool.json").write_text(json.dumps({"id": POOL}))
    monkeypatch.setattr(
        disks,
        "_run",
        lambda *args: json.dumps(
            {"filesystems": [{"fstype": "nfs4", "options": "rw,vers=4.2"}]}
        ),
    )
    original_stat = Path.stat

    def root_stat(path, *args, **kwargs):
        st = original_stat(path, *args, **kwargs)
        if path == root or path.parent == root:
            values = list(st)
            values[4] = 0
            return os.stat_result(values)
        return st

    monkeypatch.setattr(Path, "stat", root_stat)
    monkeypatch.setattr(
        disks.SharedRootfs,
        "_read_json",
        staticmethod(lambda path: json.loads(path.read_text())),
    )
    monkeypatch.setattr(disks, "sparse_copy", shutil.copyfile)
    monkeypatch.setattr(os, "chown", lambda *args: None)
    return disks.SharedRootfs(str(root))


def test_shared_root_disk_is_created_once_and_identity_is_stable(pool, tmp_path):
    source = tmp_path / "template.ext4"
    source.write_bytes(b"template-disk")
    desc = pool.create(VM, str(source), os.getuid())
    assert desc == pool.descriptor(VM)
    source.write_bytes(b"new-template")
    with pytest.raises(FileExistsError):
        pool.create(VM, str(source), os.getuid())
    assert (pool.directory / VM / "rootfs.ext4").read_bytes() == b"template-disk"


def test_shared_root_disk_rejects_symlink_and_wrong_identity(pool, tmp_path):
    with pytest.raises(ValueError):
        pool.descriptor("../../etc/passwd")
    (pool.directory / VM).symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        pool.descriptor(VM)


def test_vni_collision_is_resolved_in_shared_pool(pool, tmp_path):
    source = tmp_path / "template"
    source.write_bytes(b"disk")
    one = pool.create(VM, str(source), os.getuid())
    other = str(uuid.UUID(int=uuid.UUID(VM).int + 0xFFFFFF))
    two = pool.create(other, str(source), os.getuid())
    assert one["vni"] != two["vni"]


def test_local_filesystem_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(
        disks,
        "_run",
        lambda *args: json.dumps(
            {"filesystems": [{"fstype": "ext4", "options": "rw"}]}
        ),
    )
    # Root-owned directory in the Linux test image; on macOS mock the stat.
    original = Path.stat

    def fake_stat(path, *args, **kwargs):
        if path == tmp_path and kwargs.get("follow_symlinks", True):
            return os.stat_result((0o40755, 1, 1, 1, 0, 0, 1, 0, 0, 0))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fake_stat)
    with pytest.raises(ValueError, match="shared POSIX"):
        disks.SharedRootfs(str(tmp_path))


@pytest.mark.parametrize(
    "change",
    [
        {"vni": 0},
        {"vni": 1 << 24},
        {"local_ip": "::1"},
        {"gateway_ip": "10.90.0.1"},
        {"underlay": "wg;bad"},
        {"mtu": 1400},
    ],
)
def test_invalid_network_is_rejected(change):
    args = dict(vm_id=VM, vni=1, local_ip="10.90.0.1", gateway_ip="10.90.0.2", mtu=1500)
    args.update(change)
    with pytest.raises(ValueError):
        MigrationNetwork(**args)


def test_gateway_does_not_redirect_on_destination_prepare(tmp_path, monkeypatch):
    from flint.cluster.gateway import Gateway

    class Pool:
        pool_id = POOL

        def descriptor(self, vm):
            return {"vni": 42}

    monkeypatch.setenv("FLINT_MIGRATION_GATEWAY_IP", "10.90.0.3")
    monkeypatch.setattr(MigrationNetwork, "setup_gateway", lambda *args: "10.0.0.2")
    monkeypatch.setattr(Gateway, "namespace_exists", lambda *args: True)
    gateway = Gateway(Pool(), str(tmp_path), {"10.90.0.1", "10.90.0.2"})
    first = gateway.prepare(VM, "10.90.0.1", True)
    second = gateway.prepare(VM, "10.90.0.2", True)
    assert first == second
    assert second["peer"] == "10.90.0.1"
    with pytest.raises(ValueError):
        gateway.prepare(VM, "10.90.0.99", True)
    with pytest.raises(ValueError):
        gateway.prepare(VM, "10.90.0.2", False)


def test_starting_migration_receiver_is_fenced_before_running():
    from types import SimpleNamespace
    from flint.cluster.agent import ClusterAgent
    from flint.core.types import SandboxState

    recorded = []
    agent = ClusterAgent.__new__(ClusterAgent)
    entry = SimpleNamespace(
        vm_id=VM, pid=123, state=SandboxState.STARTING, availability="restartable"
    )
    agent.daemon = SimpleNamespace(
        manager=SimpleNamespace(_lock=threading.Lock(), _sandboxes={VM: entry})
    )
    agent._fence_lock = threading.Lock()
    agent._lease_deadline = time.monotonic() + 10
    agent.snapd = SimpleNamespace(fence=lambda ttl, vms: recorded.append((ttl, vms)))
    assert agent._arm_fence()
    assert recorded[0][1] == [{"vm_id": VM, "pid": 123}]
    agent._lease_deadline = time.monotonic() - 1
    assert not agent._arm_fence()
    assert len(recorded) == 1


def test_migration_api_refuses_unprotected_tcp():
    from flint.core import _cloud_hypervisor as ch

    with pytest.raises(ValueError, match="authenticated"):
        ch.vm_send_migration("ignored", "tcp:10.0.0.2:1234")
    with pytest.raises(ValueError, match="authenticated"):
        ch.vm_receive_migration("ignored", "tcp:0.0.0.0:1234")


def test_tls_migration_rejects_wrong_host_identity():
    import ssl
    from types import SimpleNamespace
    from flint.cluster.migration_transport import verify_peer

    conn = SimpleNamespace(
        getpeercert=lambda: {"subject": ((("commonName", "other-host"),),)}
    )
    with pytest.raises(ssl.SSLError):
        verify_peer(conn, "expected-host")


def test_authorized_recovery_creates_a_new_generation_without_overwriting_source(
    pool, tmp_path
):
    source = tmp_path / "disk"
    source.write_bytes(b"before")
    old = pool.create(VM, str(source), os.getuid())
    source.write_bytes(b"after!")
    with disks.disk_replacement(True):
        new = pool.create(VM, str(source), os.getuid())
    assert old["disk_file"] != new["disk_file"]
    assert (pool.directory / VM / old["disk_file"]).read_bytes() == b"before"
    assert (pool.directory / VM / new["disk_file"]).read_bytes() == b"after!"
    assert pool.descriptor(VM) == new
    # Explicit recovery authority never leaks into a subsequent normal create.
    with pytest.raises(FileExistsError):
        pool.create(VM, str(source), os.getuid())


def test_rehearsal_probe_records_continuity_and_connection_resets():
    import importlib.util
    import socket

    spec = importlib.util.spec_from_file_location(
        "rehearse", Path(__file__).parents[2] / "scripts/rehearse-maintenance.py"
    )
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    for disconnect in (False, True):
        server = socket.create_server(("127.0.0.1", 0))
        server.settimeout(0.1)
        stop = threading.Event()

        def echo():
            while not stop.is_set():
                try:
                    conn, _ = server.accept()
                except TimeoutError:
                    continue
                except OSError:
                    return
                with conn:
                    conn.settimeout(0.2)
                    try:
                        while data := conn.recv(4096):
                            conn.sendall(data)
                            if disconnect:
                                break
                    except OSError:
                        pass

        worker = threading.Thread(target=echo, daemon=True)
        worker.start()
        try:
            result = probe.rehearse(
                "127.0.0.1", server.getsockname()[1], 0.3, 0.01, 0.1, []
            )
            assert result["requests"] > 0
            if disconnect:
                assert result["connection_resets"] > 0
                assert result["request_failures"] > 0
            else:
                assert result["connection_resets"] == 0
                assert result["request_failures"] == 0
        finally:
            stop.set()
            server.close()
            worker.join(timeout=1)


def test_paused_source_is_never_resumed_against_receiver_disk_writes(monkeypatch):
    from types import SimpleNamespace
    from flint.cluster.live_migration import LiveMigration
    from flint.core import _cloud_hypervisor as ch

    entry = SimpleNamespace(
        socket_path="test", backend_metadata={"maintenance": {"live_phase": "sent"}}
    )
    agent = SimpleNamespace(
        daemon=SimpleNamespace(manager=SimpleNamespace(get_entry=lambda vm: entry))
    )
    migration = LiveMigration(agent)
    monkeypatch.setattr(ch, "ch_get", lambda *args: (200, b'{"state":"Paused"}'))

    def unsafe(*args):
        pytest.fail("old source RAM must not resume after possible receiver writes")

    monkeypatch.setattr(ch, "vm_resume", unsafe)
    with pytest.raises(ValueError, match="coherent checkpoint"):
        migration.rollback(VM)


def test_failed_disk_allocation_does_not_block_other_vms(pool, tmp_path, monkeypatch):
    source = tmp_path / "disk"
    source.write_bytes(b"disk")
    original = disks.sparse_copy

    def fail(*args):
        raise OSError("simulated interrupted allocation")

    monkeypatch.setattr(disks, "sparse_copy", fail)
    with pytest.raises(OSError):
        pool.create(VM, str(source), os.getuid())
    monkeypatch.setattr(disks, "sparse_copy", original)
    other = str(uuid.uuid4())
    assert pool.create(other, str(source), os.getuid())["vm_id"] == other


def test_receiver_is_not_ready_until_network_cutover_finishes(monkeypatch):
    from types import SimpleNamespace
    from flint.cluster.live_migration import LiveMigration
    from flint.core import _cloud_hypervisor as ch

    entry = SimpleNamespace(
        backend_metadata={
            "maintenance": {"live_phase": "cutover", "live_executed": True}
        },
        to_dict=lambda: {},
    )
    migration = LiveMigration(
        SimpleNamespace(
            daemon=SimpleNamespace(manager=SimpleNamespace(get_entry=lambda vm: entry))
        )
    )

    def blocked(*args):
        pytest.fail("status must not block on a VMM busy receiving migration")

    monkeypatch.setattr(ch, "ch_get", blocked)
    assert migration.status(VM)["phase"] == "cutover"
