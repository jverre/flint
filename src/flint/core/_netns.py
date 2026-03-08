import ctypes
import ctypes.util
import os
import subprocess

from pyroute2 import netns as pynetns
from pyroute2 import NetNS

from .config import HOST_TAP_MAC, HOST_IP, GUEST_IP, GUEST_MAC

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_CLONE_NEWNET = 0x40000000


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


def _setup_netns_pyroute2(ns_name: str, tap_name: str) -> None:
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


def _setup_netns_subprocess(ns_name: str, tap_name: str) -> None:
    """Create a network namespace with a configured TAP device using ip commands."""
    subprocess.run(["ip", "netns", "add", ns_name], capture_output=True, check=True)
    run_in_ns = lambda cmd: subprocess.run(
        ["ip", "netns", "exec", ns_name] + cmd, capture_output=True, text=True)
    run_in_ns(["ip", "tuntap", "add", "dev", tap_name, "mode", "tap"])
    run_in_ns(["ip", "addr", "add", f"{HOST_IP}/30", "dev", tap_name])
    run_in_ns(["ip", "link", "set", tap_name, "up"])
