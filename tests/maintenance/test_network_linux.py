"""Real kernel networking test, isolated inside the privileged test container.

The guest is a Linux namespace with an established TCP connection. Moving its
veth-backed TAP to another compute namespace models the NIC cutover while
preserving the guest's TCP stack, independently of the VMM migration protocol.
"""

import os
from pathlib import Path
import socket
import subprocess
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("FLINT_PRIVILEGED_TESTS") != "1",
    reason="requires isolated privileged Linux container",
)


def run(*args, input=None):
    result = subprocess.run(
        list(args), input=input, text=True, capture_output=True, timeout=30
    )
    if result.returncode:
        raise RuntimeError(f"{args}: {result.stderr}")
    return result.stdout.strip()


def ns(name, *args):
    return run("ip", "netns", "exec", name, *args)


def test_established_tcp_survives_compute_namespace_cutover(tmp_path):
    assert os.getuid() == 0
    names = [
        "source",
        "destination",
        "gateway",
        "compute-source",
        "compute-destination",
        "guest",
        "fmg-07ce885b-bf8a-41c3-a9c5-6dc9f09cba11",
    ]
    server = socket.socket()
    done = threading.Event()
    try:
        run("ip", "link", "add", "test-lan", "type", "bridge")
        run("ip", "link", "set", "test-lan", "mtu", "9000", "up")
        run("ip", "addr", "add", "192.168.101.1/24", "dev", "test-lan")
        run("ip", "addr", "add", "198.18.0.1/32", "dev", "lo")
        keys = {}
        for role in names[:3]:
            key = run("wg", "genkey")
            keys[role] = (key, run("wg", "pubkey", input=key + "\n"))
        for i, role in enumerate(names[:3], 1):
            run("ip", "netns", "add", role)
            run(
                "ip",
                "link",
                "add",
                "v-" + role[:5],
                "type",
                "veth",
                "peer",
                "name",
                "p-" + role[:5],
            )
            run("ip", "link", "set", "v-" + role[:5], "master", "test-lan")
            run("ip", "link", "set", "v-" + role[:5], "mtu", "9000", "up")
            run("ip", "link", "set", "p-" + role[:5], "netns", role)
            ns(role, "ip", "link", "set", "p-" + role[:5], "name", "eth0")
            ns(role, "ip", "link", "set", "eth0", "mtu", "9000", "up")
            ns(role, "ip", "link", "set", "lo", "up")
            ns(role, "ip", "addr", "add", f"192.168.101.{i + 10}/24", "dev", "eth0")
            ns(role, "ip", "route", "add", "default", "via", "192.168.101.1")
            ns(role, "ip", "link", "add", "wg-flint", "type", "wireguard")
            keyfile = tmp_path / (role + ".key")
            keyfile.write_text(keys[role][0] + "\n")
            ns(
                role,
                "wg",
                "set",
                "wg-flint",
                "private-key",
                str(keyfile),
                "listen-port",
                "51820",
            )
            for j, peer in enumerate(names[:3], 1):
                if peer == role:
                    continue
                ns(
                    role,
                    "wg",
                    "set",
                    "wg-flint",
                    "peer",
                    keys[peer][1],
                    "allowed-ips",
                    f"10.90.0.{j}/32",
                    "endpoint",
                    f"192.168.101.{j + 10}:51820",
                )
            ns(role, "ip", "addr", "add", f"10.90.0.{i}/24", "dev", "wg-flint")
            ns(role, "ip", "link", "set", "wg-flint", "mtu", "1600", "up")
        for role in names[:3]:
            run(
                "nsenter",
                "--net=/run/netns/" + role,
                "python",
                "/workspace/tests/maintenance/network_worker.py",
                role,
            )
        # Replace source TAP with a veth to a real guest TCP stack.
        run("ip", "netns", "add", "guest")
        for role in ("compute-source", "compute-destination"):
            ns(role, "ip", "link", "del", "tap0")
        ns(
            "compute-source",
            "ip",
            "link",
            "add",
            "tap0",
            "type",
            "veth",
            "peer",
            "name",
            "ethg",
        )
        ns("compute-source", "ip", "link", "set", "ethg", "netns", "guest")
        ns("compute-source", "ip", "addr", "add", "172.16.0.1/30", "dev", "tap0")
        ns("compute-source", "ip", "link", "set", "tap0", "up")
        ns("guest", "ip", "addr", "add", "172.16.0.2/30", "dev", "ethg")
        ns("guest", "ip", "link", "set", "ethg", "up")
        ns("guest", "ip", "route", "add", "default", "via", "172.16.0.1")
        server.bind(("198.18.0.1", 18080))
        server.listen()
        errors = []

        def echo():
            try:
                conn, _ = server.accept()
                conn.settimeout(15)
                with conn:
                    while data := conn.recv(4096):
                        conn.sendall(data)
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()

        threading.Thread(target=echo, daemon=True).start()
        resultfile = tmp_path / "result.json"
        client = subprocess.Popen(
            [
                "ip",
                "netns",
                "exec",
                "guest",
                "python",
                "-c",
                """
import json,socket,time,sys
s=socket.create_connection(("198.18.0.1",18080),10)
s.settimeout(10)
pauses=[]
for i in range(120):
 t=time.monotonic(); data=(str(i)+"\\n").encode(); s.sendall(data)
 assert s.recv(len(data))==data
 pauses.append(time.monotonic()-t)
 if i==0: open(sys.argv[1]+".ready","w").write("ready")
 time.sleep(.025)
s.close()
open(sys.argv[1],"w").write(json.dumps({"requests":len(pauses),"resets":0,"failures":0,"maximum_pause_s":max(pauses)}))
""",
                str(resultfile),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(100):
            if Path(str(resultfile) + ".ready").exists() or client.poll() is not None:
                break
            time.sleep(0.1)
        assert Path(str(resultfile) + ".ready").exists(), client.communicate(timeout=12)
        time.sleep(0.3)
        assert client.poll() is None, client.communicate()
        # Move the same guest NIC to the prepared destination namespace.
        ns(
            "compute-source",
            "ip",
            "link",
            "set",
            "tap0",
            "netns",
            "compute-destination",
        )
        ns("compute-destination", "ip", "link", "set", "tap0", "up")
        ns(
            "compute-destination",
            "ip",
            "addr",
            "replace",
            "172.16.0.1/30",
            "dev",
            "tap0",
        )
        gateway = "fmg-07ce885b-bf8a-41c3-a9c5-6dc9f09cba11"
        ns(
            gateway,
            "bridge",
            "fdb",
            "replace",
            "02:fc:00:00:00:2a",
            "dev",
            "uplink0",
            "dst",
            "10.90.0.2",
            "self",
        )
        ns(
            "compute-destination",
            "arping",
            "-U",
            "-c",
            "3",
            "-I",
            "uplink0",
            "192.0.2.2",
        )
        stdout, stderr = client.communicate(timeout=20)
        assert client.returncode == 0, stdout + stderr
        assert done.wait(5)
        assert not errors
        import json

        result = json.loads(resultfile.read_text())
        assert result["requests"] == 120
        assert result["maximum_pause_s"] < 5
        print("maintenance TCP rehearsal:", result)
        # Metadata/link-local egress is still denied after the move.
        assert (
            subprocess.run(
                [
                    "ip",
                    "netns",
                    "exec",
                    "guest",
                    "ping",
                    "-c",
                    "1",
                    "-W",
                    "1",
                    "169.254.169.254",
                ],
                capture_output=True,
            ).returncode
            != 0
        )
    finally:
        server.close()
        for name in reversed(names):
            subprocess.run(["ip", "netns", "del", name], capture_output=True)
        subprocess.run(["ip", "link", "del", "test-lan"], capture_output=True)
        subprocess.run(
            ["ip", "addr", "del", "198.18.0.1/32", "dev", "lo"], capture_output=True
        )


def test_single_host_tenant_boundaries_and_peer_opt_in():
    """Exercise nftables, NAT and control-port exclusions with real TCP stacks."""
    from flint.core import _netns as net
    from flint.core._limits import network_limits
    from flint.core.config import GUEST_IP, GUEST_MAC, HOST_IP, HOST_TAP_MAC, BRIDGE_IP, PROXY_PORT

    guests = []
    servers = []
    namespaces = ["fc-a0000001", "ch-a0000002", "fc-b0000001"]
    addresses = []
    errors = []

    def connects(guest, address, port):
        result = subprocess.run(["ip", "netns", "exec", guest, "python", "-c",
            "import socket,sys; s=socket.create_connection((sys.argv[1],int(sys.argv[2])),.5); s.close()",
            address, str(port)], capture_output=True, timeout=3)
        errors[:] = [result.stderr.decode()]
        return result.returncode == 0

    try:
        net.ensure_host_firewall()
        for i, namespace in enumerate(namespaces):
            address = net._setup_netns_pyroute2(namespace, "tap0")
            addresses.append(address)
            guest = f"guest-{i}"
            guests.append(guest)
            run("ip", "netns", "add", guest)
            ns(namespace, "ip", "link", "del", "tap0")
            ns(namespace, "ip", "link", "add", "tap0", "type", "veth", "peer", "name", "eth0")
            ns(namespace, "ip", "link", "set", "eth0", "netns", guest)
            ns(namespace, "ip", "link", "set", "tap0", "address", HOST_TAP_MAC, "up")
            ns(namespace, "ip", "addr", "add", HOST_IP + "/30", "dev", "tap0")
            ns(guest, "ip", "link", "set", "eth0", "address", GUEST_MAC, "up")
            ns(guest, "ip", "link", "set", "lo", "up")
            ns(guest, "ip", "addr", "add", GUEST_IP + "/30", "dev", "eth0")
            ns(guest, "ip", "route", "add", "default", "via", HOST_IP)
            network_limits(namespace, "tap0")
            # Recovery reconciles the live TAP without tearing its rules down.
            network_limits(namespace, "tap0")
            for port in (18080, 5000, PROXY_PORT):
                servers.append(subprocess.Popen(["ip", "netns", "exec", guest, "python", "-c",
                    "import socket,sys\ns=socket.socket(); s.bind((sys.argv[1],int(sys.argv[2]))); s.listen()\n"
                    "while True:\n c,a=s.accept(); c.close()", GUEST_IP, str(port)], stdout=subprocess.DEVNULL,
                                                stderr=subprocess.PIPE))
            deadline = time.monotonic() + 5
            while not connects(guest, GUEST_IP, 18080) and time.monotonic() < deadline:
                time.sleep(.02)
            assert connects(guest, GUEST_IP, 18080), (errors, ns(guest, "ss", "-ltnp"), ns(guest, "ip", "addr"))
        assert not connects(guests[0], addresses[1], 18080)
        assert not connects(guests[0], addresses[2], 18080)
        net.configure_tenant_peers([namespaces[:2], namespaces[2:]])
        assert connects(guests[0], addresses[1], 18080), "opted-in peers could not connect"
        assert connects(guests[1], addresses[0], 18080), "peer replies/routing failed"
        assert not connects(guests[0], addresses[2], 18080), "cross-tenant connection accepted"
        assert not connects(guests[2], addresses[0], 18080), "cross-tenant ingress accepted"
        for port in (5000, PROXY_PORT):
            assert not connects(guests[0], addresses[1], port), "peer exposed a control port"
        for ip in (BRIDGE_IP, "169.254.169.254", "192.168.0.1"):
            assert not connects(guests[0], ip, 18080)
        net.configure_tenant_peers([[n] for n in namespaces])
        assert not connects(guests[0], addresses[1], 18080), "revoked network still connected"
        # Rules are replaced atomically and idempotently, including proxy redirects.
        net._setup_proxy_redirect(namespaces[0])
        net._setup_proxy_redirect(namespaces[0])
        net._remove_proxy_redirect(namespaces[0])
        net.ensure_host_firewall()
    finally:
        for proc in servers:
            proc.terminate()
            proc.wait(timeout=5)
        for namespace in namespaces:
            net._delete_netns(namespace)
        for guest in guests:
            run("ip", "netns", "del", guest)
