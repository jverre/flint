import ctypes
import ctypes.util
import fcntl
import functools
import os
import socket
import struct
import subprocess
import threading

from pyroute2 import netns as pynetns
from pyroute2 import IPRoute, NetNS

from .config import (
    HOST_TAP_MAC, HOST_IP, GUEST_IP, GUEST_MAC,
    BRIDGE_NAME, BRIDGE_IP, BRIDGE_CIDR, VETH_SUBNET, PROXY_PORT, log,
)

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_CLONE_NEWNET = 0x40000000

# Every live Flint netns with a bridge veth -> its veth IP. The NFS server
# authorizes workspace exports and volume mounts by this IP, so two live namespaces must never share one.
_veth_ips: dict[str, str] = {}
_veth_next = 2
_veth_ip_lock = threading.Lock()


def _allocate_veth_ip(ns_name: str) -> str:
    """Reserve a bridge IP (10.0.0.2 .. 10.0.0.254) that no live namespace holds.

    Round-robin, so a just-released address is the last to be handed out again.
    """
    global _veth_next
    with _veth_ip_lock:
        used = set(_veth_ips.values())
        for _ in range(253):
            ip = f"{VETH_SUBNET}.{_veth_next}"
            _veth_next = _veth_next + 1 if _veth_next < 254 else 2
            if ip not in used:
                _veth_ips[ns_name] = ip
                return ip
    raise RuntimeError("No free bridge IPs: 253 network namespaces with internet access are in use")


def _release_veth_ip(ns_name: str) -> None:
    with _veth_ip_lock:
        _veth_ips.pop(ns_name, None)


def reserve_existing_veth_ips() -> None:
    """Record the veth IPs of namespaces that outlived a daemon restart.

    Without this, the allocator would hand out addresses still held by
    recovered or paused sandboxes.
    """
    result = subprocess.run(
        ["ip", "-all", "netns", "exec", "ip", "-4", "-o", "addr", "show"],
        capture_output=True, text=True,
    )
    ns = None
    found = {}
    for line in result.stdout.splitlines():
        if line.startswith("netns:"):
            ns = line.split()[1]
        elif ns and " vn-" in line:
            found[ns] = line.split()[3].split("/")[0]
    with _veth_ip_lock:
        _veth_ips.update(found)
    if found:
        log.info("Reserved %d bridge IPs held by existing namespaces", len(found))


def _strip_ns_prefix(ns_name: str) -> str:
    """Return the vm-id suffix of a Flint netns name.

    Flint wraps each VM's netns in a per-backend prefix (``fc-``, ``ch-``,
    …) so the same vm-id doesn't collide across backends on the same host.
    Shared veth/iptables helpers only need the suffix. Fall back to the
    whole name if there's no recognized prefix.
    """
    for prefix in ("fc-", "ch-"):
        if ns_name.startswith(prefix):
            return ns_name[len(prefix):]
    # Generic fallback: take everything after the first '-' if any.
    _, _, rest = ns_name.partition("-")
    return rest or ns_name


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
    _release_veth_ip(ns_name)


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


_TUNSETIFF = 0x400454CA
_IFF_TAP, _IFF_NO_PI, _IFF_VNET_HDR = 0x0002, 0x1000, 0x4000


def open_tap_fd(ns_name: str, tap_name: str) -> int:
    """Open an fd on an existing TAP inside ``ns_name`` (for VMMs that get the fd, not the name).

    Caller closes it once the VMM has received it.
    """
    orig_fd = _enter_netns(ns_name)
    try:
        # The tun device binds to the network namespace it's opened in.
        fd = os.open("/dev/net/tun", os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
    finally:
        _restore_netns(orig_fd)
    try:
        fcntl.ioctl(fd, _TUNSETIFF, struct.pack("16sH", tap_name.encode(), _IFF_TAP | _IFF_NO_PI | _IFF_VNET_HDR))
    except OSError:
        os.close(fd)
        raise
    return fd


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


def _setup_veth_pair(ns_name: str, vm_id: str) -> tuple[str, str]:
    """Create a veth pair connecting the netns to br-flint for internet access.

    Returns ``(veth_ip, veth_ns_ifname)``, e.g. ``("10.0.0.2", "vn-1a2b3c4d")``.
    """
    _ensure_bridge()

    # Linux interface names limited to 15 chars
    veth_host = f"vh-{vm_id[:8]}"
    veth_ns = f"vn-{vm_id[:8]}"
    veth_ip = _allocate_veth_ip(ns_name)

    with IPRoute() as ipr:
        # Create veth pair in root namespace
        ipr.link("add", ifname=veth_host, kind="veth", peer={"ifname": veth_ns})

        # Attach host end to bridge as an isolated port: sandboxes can talk to
        # the host (gateway, NAT) but never to each other's namespaces.
        host_idx = ipr.link_lookup(ifname=veth_host)[0]
        bridge_idx = ipr.link_lookup(ifname=BRIDGE_NAME)[0]
        ipr.link("set", index=host_idx, master=bridge_idx)
        ipr.brport("set", index=host_idx, isolated=1)
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

    log.info("veth %s <-> %s, ns IP %s", veth_host, veth_ns, veth_ip)
    return veth_ip, veth_ns


# Destinations a sandbox must never reach: the host and anything on its
# private networks, other sandboxes, and link-local (cloud metadata services
# hand out the host's credentials at 169.254.169.254).
BLOCKED_EGRESS = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.168.0.0/16", "224.0.0.0/4", "240.0.0.0/4",
)


@functools.cache
def storage_endpoints() -> tuple[tuple[str, str, int], ...]:
    """``(ip, proto, port)`` the guest must reach for storage, despite BLOCKED_EGRESS.

    Always Flint's own NFS server on the bridge (volumes, and R2 workspaces);
    plus the S3 Files endpoint when that backend is configured.
    """
    from . import config

    endpoints = [(BRIDGE_IP, "tcp", config.R2NFS_PORT)]
    if config.STORAGE_BACKEND == "s3_files" and config.S3_FILES_NFS_ENDPOINT:
        host = config.S3_FILES_NFS_ENDPOINT.split(":", 1)[0]
        ips = {info[4][0] for info in socket.getaddrinfo(host, 2049, socket.AF_INET, socket.SOCK_STREAM)}
        endpoints += [(ip, "tcp", 2049) for ip in sorted(ips)]
    return tuple(endpoints)


def _egress_rules(chain: str) -> list[str]:
    rules = [f"-A {chain} -d {ip}/32 -p {proto} --dport {port} -j RETURN" for ip, proto, port in storage_endpoints()]
    rules += [f"-A {chain} -d {cidr} -j DROP" for cidr in BLOCKED_EGRESS]
    return rules


def ensure_host_firewall() -> None:
    """Host-side rules for traffic from sandboxes on br-flint (idempotent, rebuilt each start).

    - FLINT-INPUT: sandboxes can't reach services on the host itself, except
      the storage server they mount.
    - FLINT-FORWARD: no sandbox-to-sandbox hairpin through the host, no private
      or link-local destinations, and explicit egress to the internet, so hosts
      whose FORWARD policy is DROP (Docker sets that) still give sandboxes internet.
    Each namespace enforces the same egress policy; this is the second layer.
    """
    rules = ["*filter", ":FLINT-INPUT - [0:0]", ":FLINT-FORWARD - [0:0]"]
    rules += [f"-A FLINT-INPUT -i {BRIDGE_NAME} -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT"]
    rules += [
        f"-A FLINT-INPUT -i {BRIDGE_NAME} -d {ip}/32 -p {proto} --dport {port} -j ACCEPT"
        for ip, proto, port in storage_endpoints() if ip == BRIDGE_IP
    ]
    rules += [f"-A FLINT-INPUT -i {BRIDGE_NAME} -j DROP"]
    rules += [
        f"-A FLINT-FORWARD -o {BRIDGE_NAME} -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
        f"-A FLINT-FORWARD -i {BRIDGE_NAME} -o {BRIDGE_NAME} -j DROP",
    ]
    rules += [
        f"-A FLINT-FORWARD -i {BRIDGE_NAME} -d {ip}/32 -p {proto} --dport {port} -j ACCEPT"
        for ip, proto, port in storage_endpoints() if ip != BRIDGE_IP
    ]
    rules += [f"-A FLINT-FORWARD -i {BRIDGE_NAME} -d {cidr} -j DROP" for cidr in BLOCKED_EGRESS]
    rules += [f"-A FLINT-FORWARD -i {BRIDGE_NAME} -j ACCEPT", "COMMIT", ""]
    # --noflush keeps the host's other rules; declaring the chains flushes ours.
    subprocess.run(["iptables-restore", "--noflush"], input="\n".join(rules), text=True, capture_output=True, check=True)
    for parent, chain in (("INPUT", "FLINT-INPUT"), ("FORWARD", "FLINT-FORWARD")):
        if subprocess.run(["iptables", "-C", parent, "-j", chain], capture_output=True).returncode != 0:
            subprocess.run(["iptables", "-I", parent, "1", "-j", chain], capture_output=True, check=True)


def _apply_netns_firewall(ns_name: str, veth_ns: str | None) -> None:
    """Configure forwarding, NAT and the firewall of a sandbox netns.

    Ingress: the namespace accepts and forwards nothing that arrives
    unsolicited on its bridge veth (another sandbox, a scan of the bridge
    subnet), only replies to connections the guest or its credential proxy
    opened. Applied to every namespace before a VM gets it, so pre-built ones
    are never reachable.

    Egress: nothing in BLOCKED_EGRESS except the storage endpoint.

    IPv6 is off: nothing uses it, and it would bypass these rules.
    """
    sysctls = ["net.ipv6.conf.all.disable_ipv6=1", "net.ipv6.conf.default.disable_ipv6=1"]
    rules = ["*filter", ":INPUT ACCEPT [0:0]", ":FORWARD ACCEPT [0:0]", ":OUTPUT ACCEPT [0:0]"]
    nat = ["*nat", ":PREROUTING ACCEPT [0:0]", ":INPUT ACCEPT [0:0]", ":OUTPUT ACCEPT [0:0]", ":POSTROUTING ACCEPT [0:0]"]
    if veth_ns:
        sysctls.append("net.ipv4.ip_forward=1")
        rules.insert(1, ":FLINT-EGRESS - [0:0]")
        for chain in ("INPUT", "FORWARD"):
            rules += [
                f"-A {chain} -i {veth_ns} -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
                f"-A {chain} -i {veth_ns} -j DROP",
            ]
        # Egress policy for the guest's traffic and for the namespace's own
        # (the credential proxy connects out on the guest's behalf).
        rules += [f"-A FORWARD -o {veth_ns} -j FLINT-EGRESS", f"-A OUTPUT -o {veth_ns} -j FLINT-EGRESS"]
        rules += _egress_rules("FLINT-EGRESS")
        # Masquerade guest traffic (172.16.0.0/30) going out via the veth
        nat.append(f"-A POSTROUTING -s 172.16.0.0/30 -o {veth_ns} -j MASQUERADE")
    ruleset = "\n".join(rules + ["COMMIT"] + nat + ["COMMIT"]) + "\n"
    result = subprocess.run(
        ["ip", "netns", "exec", ns_name, "sh", "-c", f"sysctl -qw {' '.join(sysctls)} && iptables-restore"],
        input=ruleset, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to configure firewall in {ns_name}: {result.stderr.strip()}")


def _setup_netns_pyroute2(ns_name: str, tap_name: str, *, internet: bool = True) -> str:
    """Create a network namespace with a configured TAP device inside it.

    Returns the veth IP address if internet access is enabled, or '' otherwise.
    """
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
    veth_ip, veth_ns = "", None
    if internet:
        veth_ip, veth_ns = _setup_veth_pair(ns_name, _strip_ns_prefix(ns_name))
    _apply_netns_firewall(ns_name, veth_ns)
    return veth_ip


def _setup_netns_subprocess(ns_name: str, tap_name: str, *, internet: bool = True) -> str:
    """Create a network namespace using ip commands.

    Returns the veth IP address if internet access is enabled, or '' otherwise.
    """
    subprocess.run(["ip", "netns", "add", ns_name], capture_output=True, check=True)
    run_in_ns = lambda cmd: subprocess.run(
        ["ip", "netns", "exec", ns_name] + cmd, capture_output=True, text=True)
    run_in_ns(["ip", "tuntap", "add", "dev", tap_name, "mode", "tap"])
    run_in_ns(["ip", "addr", "add", f"{HOST_IP}/30", "dev", tap_name])
    run_in_ns(["ip", "link", "set", tap_name, "up"])

    if not internet:
        _apply_netns_firewall(ns_name, None)
        return ""

    # Set up veth pair for internet access
    vm_id_prefix = _strip_ns_prefix(ns_name)
    _ensure_bridge()

    veth_host = f"vh-{vm_id_prefix}"
    veth_ns = f"vn-{vm_id_prefix}"
    veth_ip = _allocate_veth_ip(ns_name)

    subprocess.run(["ip", "link", "add", veth_host, "type", "veth", "peer", "name", veth_ns],
                   capture_output=True, check=True)
    subprocess.run(["ip", "link", "set", veth_host, "master", BRIDGE_NAME],
                   capture_output=True, check=True)
    subprocess.run(["bridge", "link", "set", "dev", veth_host, "isolated", "on"],
                   capture_output=True, check=True)
    subprocess.run(["ip", "link", "set", veth_host, "up"],
                   capture_output=True, check=True)
    subprocess.run(["ip", "link", "set", veth_ns, "netns", ns_name],
                   capture_output=True, check=True)
    run_in_ns(["ip", "addr", "add", f"{veth_ip}/{BRIDGE_CIDR}", "dev", veth_ns])
    run_in_ns(["ip", "link", "set", veth_ns, "up"])
    run_in_ns(["ip", "route", "add", "default", "via", BRIDGE_IP])
    _apply_netns_firewall(ns_name, veth_ns)
    return veth_ip


def _setup_proxy_redirect(ns_name: str) -> None:
    """Add iptables REDIRECT rules to send outbound HTTP/HTTPS from the guest to the credential proxy.

    Idempotent: checks whether each rule already exists before appending.
    """
    run_in_ns = lambda cmd: subprocess.run(
        ["ip", "netns", "exec", ns_name] + cmd, capture_output=True, text=True)

    for dport in ("80", "443"):
        rule = [
            "iptables", "-t", "nat",
            "-s", "172.16.0.0/30", "-p", "tcp", "--dport", dport,
            "-j", "REDIRECT", "--to-port", str(PROXY_PORT),
        ]
        # Check first, append only if missing
        check = run_in_ns(["iptables", "-t", "nat", "-C", "PREROUTING"] + rule[3:])
        if check.returncode != 0:
            run_in_ns(["iptables", "-t", "nat", "-A", "PREROUTING"] + rule[3:])

    log.info("Proxy redirect rules added for %s (port %d)", ns_name, PROXY_PORT)


def _remove_proxy_redirect(ns_name: str) -> None:
    """Remove iptables REDIRECT rules for the credential proxy."""
    run_in_ns = lambda cmd: subprocess.run(
        ["ip", "netns", "exec", ns_name] + cmd, capture_output=True, text=True)

    run_in_ns([
        "iptables", "-t", "nat", "-D", "PREROUTING",
        "-s", "172.16.0.0/30", "-p", "tcp", "--dport", "80",
        "-j", "REDIRECT", "--to-port", str(PROXY_PORT),
    ])
    run_in_ns([
        "iptables", "-t", "nat", "-D", "PREROUTING",
        "-s", "172.16.0.0/30", "-p", "tcp", "--dport", "443",
        "-j", "REDIRECT", "--to-port", str(PROXY_PORT),
    ])
    log.info("Proxy redirect rules removed for %s", ns_name)
