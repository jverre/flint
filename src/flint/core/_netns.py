import ctypes
import ctypes.util
import os
import subprocess
import threading

from pyroute2 import netns as pynetns
from pyroute2 import IPRoute, NetNS

from .config import (
    HOST_TAP_MAC, HOST_IP, GUEST_IP, GUEST_MAC,
    BRIDGE_NAME, BRIDGE_IP, BRIDGE_CIDR, VETH_SUBNET, log,
)

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_CLONE_NEWNET = 0x40000000

_veth_ip_counter = 2
_veth_ip_lock = threading.Lock()


def _allocate_veth_ip() -> str:
    """Allocate the next unique veth IP (10.0.0.2 .. 10.0.0.254)."""
    global _veth_ip_counter
    with _veth_ip_lock:
        ip = f"{VETH_SUBNET}.{_veth_ip_counter}"
        _veth_ip_counter += 1
        if _veth_ip_counter > 254:
            _veth_ip_counter = 2
        return ip


def _ns_name(vm_id: str) -> str:
    return f"fc-{vm_id[:8]}"


def _create_netns(ns_name: str) -> None:
    try:
        pynetns.create(ns_name)
    except Exception as exc:
        raise RuntimeError(f"Failed to create netns {ns_name}: {exc}") from exc


def _delete_netns(ns_name: str) -> None:
    try:
        pynetns.remove(ns_name)
    except Exception:
        pass


def _popen_in_ns(ns_name: str, cmd: list[str], **kwargs) -> subprocess.Popen:
    return subprocess.Popen(["ip", "netns", "exec", ns_name] + cmd, **kwargs)


def _enter_netns(ns_name: str) -> int:
    """Enter a network namespace. Returns fd to restore the original.
    Thread-safe: setns only affects the calling kernel thread."""
    orig_fd = os.open("/proc/self/ns/net", os.O_RDONLY)
    ns_fd = os.open(f"/var/run/netns/{ns_name}", os.O_RDONLY)
    try:
        if _libc.setns(ns_fd, _CLONE_NEWNET) != 0:
            errno = ctypes.get_errno()
            os.close(orig_fd)
            raise OSError(errno, f"setns({ns_name}): {os.strerror(errno)}")
    finally:
        os.close(ns_fd)
    return orig_fd


def _restore_netns(orig_fd: int) -> None:
    try:
        if _libc.setns(orig_fd, _CLONE_NEWNET) != 0:
            errno = ctypes.get_errno()
            raise OSError(errno, f"setns(restore): {os.strerror(errno)}")
    finally:
        os.close(orig_fd)


def _ensure_bridge() -> None:
    """Create br-flint bridge in root namespace if it doesn't already exist (idempotent)."""
    with IPRoute() as ipr:
        existing = ipr.link_lookup(ifname=BRIDGE_NAME)
        if existing:
            return

        log.info("Creating bridge %s with IP %s/%d", BRIDGE_NAME, BRIDGE_IP, BRIDGE_CIDR)

        # Create the bridge
        ipr.link("add", ifname=BRIDGE_NAME, kind="bridge")
        idx = ipr.link_lookup(ifname=BRIDGE_NAME)[0]
        ipr.addr("add", index=idx, address=BRIDGE_IP, prefixlen=BRIDGE_CIDR)
        ipr.link("set", index=idx, state="up")

    # Enable IP forwarding in root namespace
    with open("/proc/sys/net/ipv4/ip_forward", "w") as f:
        f.write("1")

    # Add masquerade rule for veth subnet (idempotent: check first, then add)
    result = subprocess.run(
        ["iptables", "-t", "nat", "-C", "POSTROUTING", "-s", f"{VETH_SUBNET}.0/{BRIDGE_CIDR}", "-j", "MASQUERADE"],
        capture_output=True,
    )
    if result.returncode != 0:
        subprocess.run(
            ["iptables", "-t", "nat", "-A", "POSTROUTING", "-s", f"{VETH_SUBNET}.0/{BRIDGE_CIDR}", "-j", "MASQUERADE"],
            capture_output=True, check=True,
        )


def _setup_veth_pair(ns_name: str, vm_id: str) -> None:
    """Create a veth pair connecting the netns to br-flint for internet access."""
    _ensure_bridge()

    # Linux interface names limited to 15 chars
    veth_host = f"vh-{vm_id[:8]}"
    veth_ns = f"vn-{vm_id[:8]}"
    veth_ip = _allocate_veth_ip()

    with IPRoute() as ipr:
        # Create veth pair in root namespace
        ipr.link("add", ifname=veth_host, kind="veth", peer={"ifname": veth_ns})

        # Attach host end to bridge and bring up
        host_idx = ipr.link_lookup(ifname=veth_host)[0]
        bridge_idx = ipr.link_lookup(ifname=BRIDGE_NAME)[0]
        ipr.link("set", index=host_idx, master=bridge_idx)
        ipr.link("set", index=host_idx, state="up")

        # Move ns end into the netns
        ns_idx = ipr.link_lookup(ifname=veth_ns)[0]
        ns_fd = os.open(f"/var/run/netns/{ns_name}", os.O_RDONLY)
        try:
            ipr.link("set", index=ns_idx, net_ns_fd=ns_fd)
        finally:
            os.close(ns_fd)

    # Configure the ns end inside the namespace
    with NetNS(ns_name, flags=0) as ns:
        idx = ns.link_lookup(ifname=veth_ns)[0]
        ns.addr("add", index=idx, address=veth_ip, prefixlen=BRIDGE_CIDR)
        ns.link("set", index=idx, state="up")
        # Default route via bridge IP
        ns.route("add", dst="default", gateway=BRIDGE_IP)

    # Enable IP forwarding inside the netns
    run_in_ns = lambda cmd: subprocess.run(
        ["ip", "netns", "exec", ns_name] + cmd, capture_output=True, text=True)
    run_in_ns(["sysctl", "-w", "net.ipv4.ip_forward=1"])

    # Masquerade guest traffic (172.16.0.0/30) going out via the veth
    run_in_ns(["iptables", "-t", "nat", "-A", "POSTROUTING", "-s", "172.16.0.0/30", "-o", veth_ns, "-j", "MASQUERADE"])

    log.info("veth %s <-> %s, ns IP %s", veth_host, veth_ns, veth_ip)


def _setup_netns_pyroute2(ns_name: str, tap_name: str, *, internet: bool = True) -> None:
    """Create a network namespace with a configured TAP device inside it."""
    _create_netns(ns_name)
    try:
        with NetNS(ns_name, flags=0) as ns:
            ns.link("add", ifname=tap_name, kind="tuntap", mode="tap")
            idx = ns.link_lookup(ifname=tap_name)[0]
            ns.link("set", index=idx, address=HOST_TAP_MAC)
            ns.addr("add", index=idx, address=HOST_IP, prefixlen=30)
            ns.link("set", index=idx, state="up")
            # Pre-seed ARP so the first SYN goes out without an ARP round-trip
            ns.neigh("add", dst=GUEST_IP, lladdr=GUEST_MAC, ifindex=idx, state=0x80)  # NUD_PERMANENT
    except Exception as exc:
        raise RuntimeError(f"Failed to setup TAP {tap_name} in {ns_name}: {exc}") from exc

    # Set up veth pair for internet access
    if internet:
        _setup_veth_pair(ns_name, ns_name.removeprefix("fc-"))


def _setup_netns_subprocess(ns_name: str, tap_name: str, *, internet: bool = True) -> None:
    """Create a network namespace with a configured TAP device using ip commands."""
    subprocess.run(["ip", "netns", "add", ns_name], capture_output=True, check=True)
    run_in_ns = lambda cmd: subprocess.run(
        ["ip", "netns", "exec", ns_name] + cmd, capture_output=True, text=True)
    run_in_ns(["ip", "tuntap", "add", "dev", tap_name, "mode", "tap"])
    run_in_ns(["ip", "addr", "add", f"{HOST_IP}/30", "dev", tap_name])
    run_in_ns(["ip", "link", "set", tap_name, "up"])

    if not internet:
        return

    # Set up veth pair for internet access
    vm_id_prefix = ns_name.removeprefix("fc-")
    _ensure_bridge()

    veth_host = f"vh-{vm_id_prefix}"
    veth_ns = f"vn-{vm_id_prefix}"
    veth_ip = _allocate_veth_ip()

    subprocess.run(["ip", "link", "add", veth_host, "type", "veth", "peer", "name", veth_ns],
                   capture_output=True, check=True)
    subprocess.run(["ip", "link", "set", veth_host, "master", BRIDGE_NAME],
                   capture_output=True, check=True)
    subprocess.run(["ip", "link", "set", veth_host, "up"],
                   capture_output=True, check=True)
    subprocess.run(["ip", "link", "set", veth_ns, "netns", ns_name],
                   capture_output=True, check=True)
    run_in_ns(["ip", "addr", "add", f"{veth_ip}/{BRIDGE_CIDR}", "dev", veth_ns])
    run_in_ns(["ip", "link", "set", veth_ns, "up"])
    run_in_ns(["ip", "route", "add", "default", "via", BRIDGE_IP])
    run_in_ns(["sysctl", "-w", "net.ipv4.ip_forward=1"])
    run_in_ns(["iptables", "-t", "nat", "-A", "POSTROUTING", "-s", "172.16.0.0/30", "-o", veth_ns, "-j", "MASQUERADE"])
