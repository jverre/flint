import ctypes
import ctypes.util
import fcntl
import functools
import json
import hashlib
import os
import socket
import struct
import subprocess
import threading

from pyroute2 import netns as pynetns
from pyroute2 import IPRoute, NetNS

from .config import (
    HOST_TAP_MAC, HOST_IP, GUEST_IP, GUEST_MAC,
    BRIDGE_NAME, BRIDGE_IP, BRIDGE_CIDR, VETH_SUBNET, PROXY_PORT,
    HOST_INPUT_CHAIN, HOST_FORWARD_CHAIN, log,
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
    Shared veth/firewall helpers only need the suffix. Fall back to the
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
    if not ns_name:
        return
    if os.path.exists(f"/var/run/netns/{ns_name}"):
        pynetns.remove(ns_name)
    if os.path.exists(f"/var/run/netns/{ns_name}"):
        raise RuntimeError(f"Namespace {ns_name} still exists; retaining its address")
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



def _setup_veth_pair(ns_name: str, vm_id: str, veth_ip: str | None = None) -> tuple[str, str]:
    """Create a veth pair connecting the netns to br-flint for internet access.

    ``veth_ip`` is an address the caller already reserved in the daemon's
    allocator (a separate process's allocator can't see live sandboxes).
    Returns ``(veth_ip, veth_ns_ifname)``, e.g. ``("10.0.0.2", "vn-1a2b3c4d")``.
    """
    _ensure_bridge()

    # Linux interface names limited to 15 chars
    veth_host = f"vh-{vm_id[:8]}"
    veth_ns = f"vn-{vm_id[:8]}"
    if veth_ip:
        with _veth_ip_lock:
            _veth_ips[ns_name] = veth_ip
    else:
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

    Always Flint's own storage server on the bridge: NFS (shared volumes, R2
    workspaces) and NBD (exclusive volumes); plus the S3 Files endpoint when
    that backend is configured.
    """
    from . import config

    endpoints = [(BRIDGE_IP, "tcp", port) for port in (config.R2NFS_PORT, config.NBD_PORT, config.NBD_RO_PORT)]
    if config.STORAGE_BACKEND == "s3_files" and config.S3_FILES_NFS_ENDPOINT:
        host = config.S3_FILES_NFS_ENDPOINT.split(":", 1)[0]
        ips = {info[4][0] for info in socket.getaddrinfo(host, 2049, socket.AF_INET, socket.SOCK_STREAM)}
        endpoints += [(ip, "tcp", 2049) for ip in sorted(ips)]
    return tuple(endpoints)


_firewall_lock = threading.RLock()
_HOST_TABLE = "flint_" + hashlib.sha256(BRIDGE_NAME.encode()).hexdigest()[:12]


def _nft(script: str, ns_name: str = "") -> None:
    cmd = (["ip", "netns", "exec", ns_name] if ns_name else []) + ["nft", "-f", "-"]
    result = subprocess.run(cmd, input=script, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"nftables in {ns_name or 'host'}: {result.stderr.strip()}")


def _replace_table(family: str, name: str, body: str, ns_name: str = "") -> None:
    # 'add table' is idempotent; delete and rebuild in one atomic transaction.
    _nft(f"add table {family} {name}\ndelete table {family} {name}\n" + body, ns_name)


def _blocked() -> str:
    return "{ " + ", ".join(BLOCKED_EGRESS) + " }"


def _retire_legacy_host_rules() -> None:
    # Upgrade from iptables-nft without flushing Docker's or the operator's rules.
    result = subprocess.run(["nft", "-j", "list", "ruleset"], check=True, text=True, capture_output=True)
    commands = []
    for obj in json.loads(result.stdout).get("nftables", []):
        rule = obj.get("rule")
        if not rule or rule.get("family") != "ip":
            continue
        expr = rule.get("expr", [])
        ours = any(e.get("jump", {}).get("target") in (HOST_INPUT_CHAIN, HOST_FORWARD_CHAIN) for e in expr)
        old_nat = rule.get("table") == "nat" and any("masquerade" in e for e in expr) and any(
            e.get("match", {}).get("right") == {"prefix": {"addr": f"{VETH_SUBNET}.0", "len": BRIDGE_CIDR}}
            for e in expr
        )
        if ours or old_nat:
            commands.append(f"delete rule ip {rule['table']} {rule['chain']} handle {rule['handle']}")
    if commands:
        _nft("\n".join(commands) + "\n")


def _docker_forward_compat() -> None:
    # Docker's iptables-nft FORWARD base chain can have policy DROP. An accept
    # in our earlier base chain cannot override that later verdict. Insert only
    # the bridge exceptions there, AFTER our own isolation filter has run.
    result = subprocess.run(["nft", "-j", "list", "chain", "ip", "filter", "FORWARD"], text=True, capture_output=True)
    if result.returncode:
        return
    comment = f"flint-forward-{BRIDGE_NAME}"
    if any(x.get("rule", {}).get("comment") == comment for x in json.loads(result.stdout).get("nftables", [])):
        return
    _nft(f'insert rule ip filter FORWARD iifname "{BRIDGE_NAME}" accept comment "{comment}"\n'
         f'insert rule ip filter FORWARD oifname "{BRIDGE_NAME}" accept comment "{comment}"\n')


def ensure_host_firewall() -> None:
    """Atomic native nftables isolation, NAT and a default-empty peer set."""
    storage = "\n".join(f'iifname "{BRIDGE_NAME}" ip daddr {ip} {proto} dport {port} accept'
                        for ip, proto, port in storage_endpoints())
    body = f'''table inet {_HOST_TABLE} {{
      set peers {{ type ipv4_addr . ipv4_addr; }}
      chain input {{ type filter hook input priority -200; policy accept;
        iifname "{BRIDGE_NAME}" meta nfproto ipv6 drop
        iifname "{BRIDGE_NAME}" ct state established,related accept
        {storage}
        iifname "{BRIDGE_NAME}" drop
      }}
      chain forward {{ type filter hook forward priority -200; policy accept;
        iifname "{BRIDGE_NAME}" meta nfproto ipv6 drop
        iifname "{BRIDGE_NAME}" oifname "{BRIDGE_NAME}" ip saddr . ip daddr @peers accept
        iifname "{BRIDGE_NAME}" oifname "{BRIDGE_NAME}" drop
        oifname "{BRIDGE_NAME}" ct state established,related accept
        {storage}
        iifname "{BRIDGE_NAME}" ip daddr {_blocked()} drop
        iifname "{BRIDGE_NAME}" accept
      }}
      chain postrouting {{ type nat hook postrouting priority srcnat; policy accept;
        ip saddr {VETH_SUBNET}.0/{BRIDGE_CIDR} oifname != "{BRIDGE_NAME}" masquerade
      }}
    }}\n'''
    with _firewall_lock:
        _replace_table("inet", _HOST_TABLE, body)
        _retire_legacy_host_rules()
        _docker_forward_compat()


def _apply_netns_firewall(ns_name: str, veth_ns: str | None, *, allow_storage: bool = True) -> None:
    """Default deny unsolicited ingress; peers may reach guest services only."""
    subprocess.run(["ip", "netns", "exec", ns_name, "sysctl", "-qw",
                    "net.ipv6.conf.all.disable_ipv6=1", "net.ipv6.conf.default.disable_ipv6=1",
                    "net.ipv4.ip_forward=1"], check=True, capture_output=True)
    # Remove only legacy tables inside this VM's private namespace on upgrade.
    for table in ("filter", "nat"):
        subprocess.run(["ip", "netns", "exec", ns_name, "nft", "delete", "table", "ip", table], capture_output=True)
    storage = "\n".join(f"ip daddr {ip} {proto} dport {port} accept" for ip, proto, port in storage_endpoints()) if allow_storage else ""
    ingress = forwarding = nat = ""
    if veth_ns:
        address = _veth_ips[ns_name]
        ingress = f'iifname "{veth_ns}" ct state established,related accept\niifname "{veth_ns}" drop'
        forwarding = f'''iifname "{veth_ns}" ct state established,related accept
          iifname "{veth_ns}" ip saddr @peers ip daddr {GUEST_IP} accept
          iifname "{veth_ns}" drop
          ip saddr != {GUEST_IP} drop
          oifname "{veth_ns}" jump egress'''
        # Reserved ports are never DNATed: packets land in INPUT and are dropped.
        nat = f'''chain prerouting {{ type nat hook prerouting priority dstnat; policy accept;
          iifname "{veth_ns}" meta l4proto tcp tcp dport {{ 5000, {PROXY_PORT} }} return
          iifname "{veth_ns}" ip saddr @peers ip daddr {address} dnat ip to {GUEST_IP}
        }}
        chain postrouting {{ type nat hook postrouting priority srcnat; policy accept;
          ip saddr {GUEST_IP} oifname "{veth_ns}" masquerade
        }}'''
    body = f'''table inet flint {{
      set peers {{ type ipv4_addr; }}
      chain egress {{
        ip daddr @peers accept
        {storage}
        ip daddr {_blocked()} drop
      }}
      chain input {{ type filter hook input priority filter; policy accept;
        {ingress}
      }}
      chain forward {{ type filter hook forward priority filter; policy accept;
        {forwarding}
      }}
      chain output {{ type filter hook output priority filter; policy accept;
        {f'oifname "{veth_ns}" jump egress' if veth_ns else ''}
      }}
      {nat}
    }}\n'''
    _replace_table("inet", "flint", body, ns_name)


def configure_tenant_peers(groups: list[list[str]]) -> None:
    """Replace the host ACL first (revocation), then the netns sets/routes.

    Bridge ports remain isolated; /32 routes force peers through the gateway,
    whose source/destination ACL is enforced even on established connections.
    """
    import ipaddress
    with _firewall_lock:
        addresses = {ns: ip for group in groups for ns in group if (ip := _veth_ips.get(ns))}
        pairs = [(addresses[a], addresses[b]) for group in groups for a in group for b in group
                 if a != b and a in addresses and b in addresses]
        script = f"flush set inet {_HOST_TABLE} peers\n"
        if pairs:
            script += f"add element inet {_HOST_TABLE} peers {{ " + ", ".join(f"{a} . {b}" for a, b in pairs) + " }\n"
        _nft(script)
        for group in groups:
            for ns_name in group:
                if ns_name not in addresses or not os.path.exists(f"/var/run/netns/{ns_name}"):
                    continue
                peers = [str(ipaddress.IPv4Address(addresses[n])) for n in group if n != ns_name and n in addresses]
                script = "flush set inet flint peers\n"
                if peers:
                    script += "add element inet flint peers { " + ", ".join(peers) + " }\n"
                _nft(script, ns_name)
                for ip in peers:
                    subprocess.run(["ip", "-n", ns_name, "route", "replace", ip + "/32", "via", BRIDGE_IP],
                                   check=True, capture_output=True)


def _setup_netns_pyroute2(ns_name: str, tap_name: str, *, internet: bool = True,
                          veth_ip: str | None = None) -> str:
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
    veth_ns = None
    if internet:
        veth_ip, veth_ns = _setup_veth_pair(ns_name, _strip_ns_prefix(ns_name), veth_ip)
    else:
        veth_ip = ""
    _apply_netns_firewall(ns_name, veth_ns)
    from ._limits import network_limits
    network_limits(ns_name, tap_name)
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
        from ._limits import network_limits
        network_limits(ns_name, tap_name)
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
    from ._limits import network_limits
    network_limits(ns_name, tap_name)
    return veth_ip


def _setup_proxy_redirect(ns_name: str) -> None:
    # Peer traffic is never fed to the credential proxy. Its INPUT remains
    # guest-facing only, and peer DNAT excludes the proxy and agent ports.
    body = f'''table inet flint_proxy {{
      chain prerouting {{ type nat hook prerouting priority -110; policy accept;
        ip daddr {VETH_SUBNET}.0/{BRIDGE_CIDR} return
        ip saddr {GUEST_IP} tcp dport {{ 80, 443 }} redirect to :{PROXY_PORT}
      }}
    }}\n'''
    _replace_table("inet", "flint_proxy", body, ns_name)


def _remove_proxy_redirect(ns_name: str) -> None:
    subprocess.run(["ip", "netns", "exec", ns_name, "nft", "delete", "table", "inet", "flint_proxy"],
                   check=True, capture_output=True)


def reconcile_namespace(ns_name: str, *, proxy: bool = False) -> None:
    """Upgrade a recovered namespace and reapply its host-enforced ceilings."""
    from ._limits import network_limits
    result = subprocess.run(["ip", "-n", ns_name, "-j", "-4", "addr", "show"],
                            check=True, text=True, capture_output=True)
    links = json.loads(result.stdout)
    tap = next(link["ifname"] for link in links if any(a.get("local") == HOST_IP for a in link.get("addr_info", [])))
    veth = next((link["ifname"] for link in links if link["ifname"].startswith("vn-")), None)
    _apply_netns_firewall(ns_name, veth)
    network_limits(ns_name, tap)
    if proxy:
        _setup_proxy_redirect(ns_name)
