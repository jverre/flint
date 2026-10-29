"""Per-VM routed VXLAN uplinks to a stable, separate egress gateway.

Only the gateway owns NAT/conntrack. Compute namespaces contain a TAP and
stateless routing; recreating one on another host preserves the guest's IP,
MAC and external NAT tuple. VXLAN runs over a required WireGuard underlay.
Each VM gets its own VNI and gateway namespace, including identical guest IPs.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
import subprocess
import uuid

from ..config import GUEST_IP, GUEST_MAC, HOST_IP, HOST_TAP_MAC
from .._identities import vm_identity
from .netns import BLOCKED_EGRESS, _setup_veth_pair, _delete_netns


def run(*argv: str, input: str | None = None) -> str:
    return subprocess.run(
        list(argv), input=input, check=True, capture_output=True, text=True, timeout=30
    ).stdout


@dataclass(frozen=True)
class MigrationNetwork:
    vm_id: str
    vni: int
    local_ip: str
    gateway_ip: str
    underlay: str = "wg-flint"
    mtu: int = 1500

    def __post_init__(self):
        if str(uuid.UUID(self.vm_id)) != self.vm_id:
            raise ValueError("canonical VM UUID required")
        if not 1 <= self.vni <= 0xFFFFFF:
            raise ValueError("VXLAN ID must be 1..16777215")
        for address in (self.local_ip, self.gateway_ip):
            if ipaddress.ip_address(address).version != 4:
                raise ValueError("migration underlay requires IPv4")
        if self.local_ip == self.gateway_ip:
            raise ValueError("gateway must be separate from the compute host")
        if (
            not self.underlay
            or len(self.underlay) > 15
            or not self.underlay.replace("-", "").isalnum()
        ):
            raise ValueError("invalid underlay interface")
        # Keep guest MTU (1500) support by requiring a large enough underlay;
        # fragmenting outer IPv4 packets is also possible but less predictable.
        if self.mtu < 1500 or self.mtu > 8900:
            raise ValueError("migration uplink MTU must be 1500..8900")

    @property
    def namespace(self) -> str:
        return f"chm-{self.vm_id}"

    @property
    def mac(self) -> str:
        return "02:fc:00:" + ":".join(f"{self.vni:06x}"[i : i + 2] for i in (0, 2, 4))

    def _tunnel(self, namespace: str, gateway: bool) -> None:
        # Create the tunnel in root's network namespace before moving it: its
        # UDP socket and WireGuard route stay in the protected underlay.
        import json

        links = json.loads(run("ip", "-j", "-d", "link", "show", "dev", self.underlay))
        if links[0].get("linkinfo", {}).get("info_kind") != "wireguard":
            raise ValueError("migration underlay must be a WireGuard interface")
        if links[0]["mtu"] < self.mtu + 50:
            raise ValueError("WireGuard MTU must cover VXLAN plus guest MTU")
        name = f"fm{'g' if gateway else 'c'}{self.vni:06x}"
        local, remote = (
            (self.gateway_ip, self.local_ip)
            if gateway
            else (self.local_ip, self.gateway_ip)
        )
        run(
            "ip",
            "link",
            "add",
            name,
            "type",
            "vxlan",
            "id",
            str(self.vni),
            "local",
            local,
            "remote",
            remote,
            "dev",
            self.underlay,
            "dstport",
            "4789",
            "ageing",
            "0",
        )
        try:
            run("ip", "link", "set", name, "netns", namespace)
        except Exception:
            run("ip", "link", "del", name)
            raise
        self._ns(namespace, "ip", "link", "set", name, "name", "uplink0")
        self._ns(namespace, "ip", "link", "set", "uplink0", "mtu", str(self.mtu))
        if not gateway:
            self._ns(namespace, "ip", "link", "set", "uplink0", "address", self.mac)
        self._ns(
            namespace,
            "ip",
            "addr",
            "add",
            "192.0.2.1/30" if gateway else "192.0.2.2/30",
            "dev",
            "uplink0",
        )
        self._ns(namespace, "ip", "link", "set", "uplink0", "up")

    @staticmethod
    def _ns(namespace: str, *argv: str, input: str | None = None) -> str:
        return run("ip", "netns", "exec", namespace, *argv, input=input)

    def setup_compute(self, internet: bool) -> tuple[str, str]:
        ns = self.namespace
        run("ip", "netns", "add", ns)
        try:
            self._tunnel(ns, False)
            self._ns(
                ns,
                "ip",
                "tuntap",
                "add",
                "dev",
                "tap0",
                "mode",
                "tap",
                "vnet_hdr",
                "user",
                str(vm_identity(self.vm_id)[0]),
            )
            self._ns(ns, "ip", "link", "set", "tap0", "address", HOST_TAP_MAC)
            self._ns(ns, "ip", "addr", "add", f"{HOST_IP}/30", "dev", "tap0")
            self._ns(ns, "ip", "link", "set", "tap0", "mtu", "1500", "up")
            self._ns(
                ns,
                "ip",
                "neigh",
                "replace",
                GUEST_IP,
                "lladdr",
                GUEST_MAC,
                "dev",
                "tap0",
                "nud",
                "permanent",
            )
            self._ns(ns, "ip", "route", "add", "default", "via", "192.0.2.1")
            self._sysctls(ns)
            # No NAT or stateful filtering here. The anchored gateway admits
            # replies; the migrated namespace need not carry conntrack state.
            blocked = "{ " + ", ".join(BLOCKED_EGRESS) + " }"
            rules = f'''table inet flint_migration {{
              chain input {{ type filter hook input priority filter; policy drop;
                iifname {{ "lo", "tap0" }} accept
              }}
              chain forward {{ type filter hook forward priority filter; policy drop;
                iifname "uplink0" oifname "tap0" ip daddr {GUEST_IP} accept
                iifname "tap0" ip daddr {blocked} drop
                {f'iifname "tap0" oifname "uplink0" ip saddr {GUEST_IP} accept' if internet else ''}
              }}
              chain output {{ type filter hook output priority filter; policy accept;
                oifname "uplink0" ip daddr {blocked} drop
              }}
            }}\n'''
            self._ns(ns, "nft", "-f", "-", input=rules)
            from .._limits import network_limits
            network_limits(ns, "tap0")
            return ns, "tap0"
        except Exception:
            _delete_netns(ns)
            raise

    @staticmethod
    def _sysctls(ns: str) -> None:
        MigrationNetwork._ns(
            ns,
            "sysctl",
            "-qw",
            "net.ipv4.ip_forward=1",
            "net.ipv4.conf.all.rp_filter=0",
            "net.ipv6.conf.all.disable_ipv6=1",
            "net.ipv6.conf.default.disable_ipv6=1",
        )

    def setup_gateway(self, internet: bool) -> str:
        ns = f"fmg-{self.vm_id}"
        run("ip", "netns", "add", ns)
        try:
            self._tunnel(ns, True)
            address, veth = _setup_veth_pair(ns, f"{self.vni:06x}")
            self._sysctls(ns)
            self._ns(ns, "ip", "route", "add", f"{GUEST_IP}/32", "via", "192.0.2.2")
            self._ns(
                ns,
                "ip",
                "neigh",
                "replace",
                "192.0.2.2",
                "lladdr",
                self.mac,
                "dev",
                "uplink0",
                "nud",
                "permanent",
            )
            self._ns(
                ns,
                "bridge",
                "fdb",
                "replace",
                self.mac,
                "dev",
                "uplink0",
                "dst",
                self.local_ip,
                "self",
            )
            blocked = "{ " + ", ".join(BLOCKED_EGRESS) + " }"
            rules = f'''table inet flint_gateway {{
              chain input {{ type filter hook input priority filter; policy drop; }}
              chain forward {{ type filter hook forward priority filter; policy drop;
                iifname "{veth}" oifname "uplink0" ip daddr {GUEST_IP} ct state established,related accept
                iifname "uplink0" ip daddr {blocked} drop
                {f'iifname "uplink0" oifname "{veth}" ip saddr {GUEST_IP} accept' if internet else ''}
              }}
              chain postrouting {{ type nat hook postrouting priority srcnat; policy accept;
                ip saddr {GUEST_IP} oifname "{veth}" masquerade
              }}
            }}\n'''
            self._ns(ns, "nft", "-f", "-", input=rules)
            return address
        except Exception:
            _delete_netns(ns)
            raise

    def announce(self) -> None:
        # Stable transit MAC/IP moves to this tunnel endpoint. The gateway's
        # VXLAN FDB learns the new remote endpoint from this announcement.
        self._ns(
            self.namespace, "arping", "-U", "-c", "3", "-I", "uplink0", "192.0.2.2"
        )


def configured_network(vm_id: str, vni: int) -> MigrationNetwork:
    return MigrationNetwork(
        vm_id,
        vni,
        os.environ["FLINT_MIGRATION_LOCAL_IP"],
        os.environ["FLINT_MIGRATION_GATEWAY_IP"],
        os.environ.get("FLINT_MIGRATION_UNDERLAY", "wg-flint"),
        int(os.environ.get("FLINT_MIGRATION_MTU", "1500")),
    )
