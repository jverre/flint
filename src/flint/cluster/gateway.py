"""Stable migration gateway API. Run on a dedicated Linux gateway, not a compute host.

Uses host mTLS identities and a shared pool's immutable VM/VNI descriptors.
WireGuard peer IPs are explicitly allowlisted. Repeated prepare is idempotent;
activation changes only VXLAN forwarding, leaving NAT/conntrack untouched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time

import httpx

from flint.core._migration_network import MigrationNetwork
from flint.core._shared_rootfs import SharedRootfs


class Gateway:
    def __init__(self, pool: SharedRootfs, directory: str, peers: set[str]) -> None:
        self.pool = pool
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.peers = peers
        self.lock = threading.Lock()

    @staticmethod
    def namespace_exists(vm_id: str) -> bool:
        return Path(f"/run/netns/fmg-{vm_id}").exists()

    def prepare(self, vm_id: str, peer: str, internet: bool) -> dict:
        if peer not in self.peers:
            raise ValueError("compute endpoint is not an allowed WireGuard peer")
        disk = self.pool.descriptor(vm_id)
        net = MigrationNetwork(
            vm_id,
            disk["vni"],
            peer,
            os.environ["FLINT_MIGRATION_GATEWAY_IP"],
            os.environ.get("FLINT_MIGRATION_UNDERLAY", "wg-flint"),
            int(os.environ.get("FLINT_MIGRATION_MTU", "1500")),
        )
        path = self.directory / f"{vm_id}.json"
        with self.lock:
            if path.exists():
                if not self.namespace_exists(vm_id):
                    raise ValueError(
                        "gateway namespace lost; recover the gateway before retrying"
                    )
                result = json.loads(path.read_text())
                if (
                    result["pool_id"] != self.pool.pool_id
                    or result["internet"] != internet
                ):
                    raise ValueError("gateway identity or internet policy mismatch")
                # A destination prepare must never redirect the source's traffic.
                return result
            address = net.setup_gateway(internet)
            result = {
                "pool_id": self.pool.pool_id,
                "vni": disk["vni"],
                "internet": internet,
                "peer": peer,
                "egress_address": address,
            }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(result))
            tmp.replace(path)
            return result

    def collect(self) -> None:
        # Fail closed if the control plane is unavailable. Never reclaim a VM
        # merely because its compute agent missed a heartbeat.
        from flint.core import config
        import ssl

        identity = config.CLUSTER_IDENTITY_DIR
        server = json.loads(Path(f"{identity}/host.json").read_text())["server"].rstrip(
            "/"
        )
        ctx = ssl.create_default_context(cafile=f"{identity}/cp-ca.pem")
        ctx.load_cert_chain(f"{identity}/host.crt", f"{identity}/host.key")
        with httpx.Client(verify=ctx, timeout=5) as client:
            for path in self.directory.glob("*.json"):
                vm_id = path.stem
                self.pool.descriptor(vm_id)
                response = client.post(
                    server + "/v1/hosts/self/network-release", json={"vm_id": vm_id}
                )
                response.raise_for_status()
                if response.json().get("allowed") is not True:
                    continue
                with self.lock:
                    namespace = f"fmg-{vm_id}"
                    if self.namespace_exists(vm_id):
                        from flint.core._migration_network import run
                        from flint.core._netns import _release_veth_ip

                        run("ip", "netns", "del", namespace)
                        _release_veth_ip(namespace)
                    path.unlink()

    def activate(self, vm_id: str, peer: str) -> dict:
        if peer not in self.peers:
            raise ValueError("compute endpoint is not an allowed WireGuard peer")
        self.pool.descriptor(vm_id)
        with self.lock:
            path = self.directory / f"{vm_id}.json"
            result = json.loads(path.read_text())
            mac = "02:fc:00:" + ":".join(
                f"{result['vni']:06x}"[i : i + 2] for i in (0, 2, 4)
            )
            MigrationNetwork._ns(
                f"fmg-{vm_id}",
                "bridge",
                "fdb",
                "replace",
                mac,
                "dev",
                "uplink0",
                "dst",
                peer,
                "self",
            )
            result["peer"] = peer
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(result))
            tmp.replace(path)
            return result


def gateway_request(action: str, vm_id: str, *, internet: bool = True) -> dict:
    from flint.core import config

    identity = config.CLUSTER_IDENTITY_DIR
    url = os.environ["FLINT_MIGRATION_GATEWAY_API"].rstrip("/")
    if not url.startswith("https://"):
        raise ValueError("migration gateway requires HTTPS with mutual TLS")
    import ssl

    ctx = ssl.create_default_context(cafile=f"{identity}/hosts-ca.pem")
    ctx.load_cert_chain(f"{identity}/host.crt", f"{identity}/host.key")
    with httpx.Client(verify=ctx, timeout=30) as client:
        response = client.post(
            f"{url}/v1/networks/{vm_id}/{action}",
            json={"peer": os.environ["FLINT_MIGRATION_LOCAL_IP"], "internet": internet},
        )
        response.raise_for_status()
        return response.json()


def main() -> None:
    import ssl
    import uvicorn
    from fastapi import FastAPI, HTTPException
    from flint.core._netns import ensure_host_firewall, reserve_existing_veth_ips

    reserve_existing_veth_ips()
    ensure_host_firewall()
    gateway = Gateway(
        SharedRootfs(os.environ["FLINT_SHARED_ROOTFS_DIR"]),
        os.environ.get("FLINT_GATEWAY_STATE_DIR", "/var/lib/flint/gateway"),
        set(os.environ["FLINT_MIGRATION_PEERS"].split(",")),
    )

    def collect():
        import logging

        while True:
            try:
                gateway.collect()
            except Exception as exc:
                logging.getLogger("flint").warning(
                    "gateway collection deferred: %s", type(exc).__name__
                )
            time.sleep(30)

    threading.Thread(target=collect, daemon=True, name="gateway-collection").start()
    app = FastAPI()

    @app.post("/v1/networks/{vm_id}/{action}")
    def network(vm_id: str, action: str, body: dict):
        try:
            if action == "prepare":
                return gateway.prepare(vm_id, body["peer"], body["internet"])
            if action == "activate":
                return gateway.activate(vm_id, body["peer"])
            raise ValueError("unknown network action")
        except (KeyError, ValueError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc

    from flint.core import config

    identity = config.CLUSTER_IDENTITY_DIR
    uvicorn.run(
        app,
        host=os.environ.get("FLINT_GATEWAY_LISTEN", "0.0.0.0"),
        port=int(os.environ.get("FLINT_GATEWAY_PORT", "9600")),
        ssl_keyfile=f"{identity}/host.key",
        ssl_certfile=f"{identity}/host.crt",
        ssl_ca_certs=f"{identity}/hosts-ca.pem",
        ssl_cert_reqs=ssl.CERT_REQUIRED,
    )


if __name__ == "__main__":
    main()
