"""A real Flint cluster on one machine, for the cluster e2e tests.

- Postgres and an S3 store (SeaweedFS) in Docker, unless FLINT_TEST_DATABASE_URL
  / FLINT_TEST_STORE_URL point at existing ones (sqlite:// and file:// work too).
- Two flint-server replicas behind a TCP load balancer (one for the API, one
  for host traffic), so a replica can be killed mid-test.
- Hosts: each a `flint agent` + `flint-cluster snapd` pair with its own data
  dirs, bridge, subnet, ports and cgroups, so several share this machine.

Everything listens on 127.0.0.1. Needs root (VMs), KVM, Go and Docker.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.request

import httpx

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ADMIN_TOKEN = "flint_testadmin_" + "c3VwZXJzZWNyZXQ"
PIDS_DIR = ""  # set by Cluster: processes are recorded so a later run can clean up after a crashed one


def kill_recorded(pids_dir: str) -> None:
    """Kill processes a previous run started (it may have died before teardown)."""
    if not os.path.isdir(pids_dir):
        return
    for name in os.listdir(pids_dir):
        try:
            with open(os.path.join(pids_dir, name)) as f:
                pid = int(f.read().strip())
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read()
            if b"flint" in cmd:
                os.kill(pid, signal.SIGKILL)
        except (OSError, ValueError):
            pass
        os.unlink(os.path.join(pids_dir, name))


def wait_for(cond, timeout: float, what: str, interval: float = 0.25):
    """Poll cond() until truthy; return its value or fail with what timed out."""
    deadline = time.time() + timeout
    last_exc = None
    while time.time() < deadline:
        try:
            v = cond()
            if v:
                return v
        except Exception as e:  # noqa: BLE001 - polled conditions may raise until ready
            last_exc = e
        time.sleep(interval)
    raise TimeoutError(f"timed out after {timeout}s waiting for {what}" + (f" (last error: {last_exc})" if last_exc else ""))


def tail(path: str, n: int = 40) -> str:
    try:
        with open(path, errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return "(no log)"


class Balancer:
    """Round-robin TCP proxy that skips backends refusing connections."""

    def __init__(self, port: int, backends: list[int]) -> None:
        self.port, self.backends = port, backends
        self._i = 0
        self._srv = socket.create_server(("0.0.0.0", port), reuse_port=False)
        self._srv.settimeout(0.5)
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while not self._stop:
            try:
                c, _ = self._srv.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c: socket.socket) -> None:
        up = None
        for _ in range(len(self.backends)):
            port = self.backends[self._i % len(self.backends)]
            self._i += 1
            try:
                up = socket.create_connection(("127.0.0.1", port), timeout=2)
                break
            except OSError:
                continue
        if up is None:
            c.close()
            return
        up.settimeout(None)

        def pipe(a, b):
            try:
                while True:
                    data = a.recv(65536)
                    if not data:
                        break
                    b.sendall(data)
            except OSError:
                pass
            finally:
                for s in (a, b):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        threading.Thread(target=pipe, args=(c, up), daemon=True).start()
        pipe(up, c)

    def close(self) -> None:
        self._stop = True
        self._srv.close()


class Proc:
    def __init__(self, name: str, cmd: list[str], env: dict, log: str) -> None:
        self.name, self.cmd, self.env, self.log = name, cmd, env, log
        self.p: subprocess.Popen | None = None

    def start(self) -> None:
        fd = open(self.log, "ab")
        self.p = subprocess.Popen(self.cmd, env=self.env, stdout=fd, stderr=subprocess.STDOUT, start_new_session=True)
        if PIDS_DIR:
            with open(os.path.join(PIDS_DIR, f"{self.name}.pid"), "w") as f:
                f.write(str(self.p.pid))

    def alive(self) -> bool:
        return self.p is not None and self.p.poll() is None

    def signal(self, sig: int) -> None:
        if self.alive():
            os.kill(self.p.pid, sig)

    def stop(self, timeout: float = 15) -> None:
        if not self.alive():
            return
        self.p.terminate()
        try:
            self.p.wait(timeout)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait(5)

    def kill(self) -> None:
        if self.alive():
            self.p.kill()
            self.p.wait(5)


class Host:
    """One cluster host: agent + snapd."""

    def __init__(self, cluster: "Cluster", name: str, idx: int, zone: str) -> None:
        self.c, self.name, self.idx, self.zone = cluster, name, idx, zone
        self.root = os.path.join(cluster.base, "hosts", name)
        self.agent_port = cluster.port + 40 + idx
        self.snapd_port = cluster.port + 60 + idx
        self.bridge = f"br-tc{cluster.port % 100}{idx}"[:15]
        self.subnet = f"10.{200 + (cluster.port % 50)}.{idx}"
        self.cgroup = f"flint-tc-{name}"
        self.socket = os.path.join(self.root, "snapd.sock")
        self.identity = os.path.join(self.root, "state", "cluster")
        self.agent: Proc | None = None
        self.snapd: Proc | None = None
        self.host_id = ""

    def env(self) -> dict:
        e = dict(os.environ)
        e.update({
            "PYTHONUNBUFFERED": "1",
            "FLINT_BACKEND": self.c.backend,
            "FLINT_DATA_DIR": f"{self.root}/data", "FLINT_STATE_DIR": f"{self.root}/state",
            "FLINT_JAILER_BASE_DIR": f"{self.root}/jailer",
            "FLINT_BRIDGE_NAME": self.bridge, "FLINT_BRIDGE_SUBNET": self.subnet,
            "FLINT_R2NFS_PORT": str(self.c.port + 80 + self.idx), "FLINT_R2NFS_MGMT_PORT": str(self.c.port + 90 + self.idx),
            "FLINT_JAILER_PARENT_CGROUP": self.cgroup, "FLINT_CH_PARENT_CGROUP": f"{self.cgroup}-ch",
            "FLINT_SNAPD_SOCKET": self.socket, "FLINT_CLUSTER_BIN": self.c.binary,
            "FLINT_NETNS_POOL_SIZE": "2",
        })
        e.update(self.c.store_env)
        for k in ("FLINT_URL", "FLINT_API_KEY", "FLINT_TENANT", "FLINT_PORT", "FLINT_HOST"):
            e.pop(k, None)
        return e

    def start(self, token: str | None = None) -> None:
        for d in ("data", "state", "jailer", "cache"):
            os.makedirs(os.path.join(self.root, d), exist_ok=True)
        # Two pre-copied disks per host, not eight: a test host makes few VMs at once.
        with open(os.path.join(self.root, "state", "config.json"), "w") as f:
            json.dump({"pool_target_size": 2}, f)
        cmd = [sys.executable, "-c", "from flint.cli import cli; cli()", "agent",
               # All interfaces, as in production: the isolation test checks VMs still can't reach it.
               "--advertise", "127.0.0.1", "--listen", "0.0.0.0", "--port", str(self.agent_port),
               "--snapd-port", str(self.snapd_port), "--name", self.name, "--zone", self.zone]
        if token:
            cmd += ["--join", token]
        self.agent = Proc(f"agent-{self.name}", cmd, self.env(), os.path.join(self.c.logs, f"agent-{self.name}.log"))
        self.agent.start()
        wait_for(lambda: os.path.exists(os.path.join(self.identity, "host.json")) or not self.agent.alive(),
                 120, f"host {self.name} to join")
        if not self.agent.alive():
            raise RuntimeError(f"agent {self.name} exited:\n{tail(self.agent.log)}")
        with open(os.path.join(self.identity, "host.json")) as f:
            self.host_id = json.load(f)["host_id"]
        self.snapd = Proc(f"snapd-{self.name}", [
            self.c.binary, "snapd", "--identity", self.identity, "--cache", f"{self.root}/cache", "--cache-gb", "2",
            "--store", self.c.store_url, "--socket", self.socket, "--peer-listen", f"0.0.0.0:{self.snapd_port}",
        ], self.env(), os.path.join(self.c.logs, f"snapd-{self.name}.log"))
        self.snapd.start()
        wait_for(lambda: self.state() == "ready" or not self.agent.alive(), 600, f"host {self.name} to be ready")
        if not self.agent.alive():
            raise RuntimeError(f"agent {self.name} exited:\n{tail(self.agent.log)}")

    def state(self) -> str:
        r = self.c.api.get(f"/v1/hosts/{self.host_id}")
        return r.json()["host"]["state"] if r.status_code == 200 else ""

    def info(self) -> dict:
        return self.c.api.get(f"/v1/hosts/{self.host_id}").json()["host"]

    def vmm_pids(self) -> list[int]:
        """VMM processes of this host's VMs: pids from its state store, confirmed
        by the VM ID in their command line (Firecracker's --id) or cgroup (Cloud
        Hypervisor), so a reused pid is never killed."""
        import sqlite3

        try:
            db = sqlite3.connect(f"file:{self.root}/state/flint.db?mode=ro", uri=True)
            rows = db.execute("SELECT vm_id, pid FROM sandboxes WHERE state != 'Dead'").fetchall()
            db.close()
        except sqlite3.Error:
            return []
        pids = []
        for vm_id, pid in rows:
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read().decode(errors="replace")
                with open(f"/proc/{pid}/cgroup") as f:
                    cg = f.read()
            except OSError:
                continue
            if vm_id in cmd or vm_id in cg:
                pids.append(pid)
        return pids

    def crash(self) -> None:
        """Power off: agent, snapd and every VM die at once."""
        for p in (self.agent, self.snapd):
            if p:
                p.kill()
        for pid in self.vmm_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        wait_for(lambda: not self.vmm_pids(), 10, f"host {self.name}'s VMs to die")

    def freeze(self) -> None:
        self.agent.signal(signal.SIGSTOP)

    def thaw(self) -> None:
        self.agent.signal(signal.SIGCONT)

    def ensure_up(self) -> None:
        """Bring a crashed or frozen host back (its identity is kept)."""
        if self.agent and self.agent.alive():
            self.thaw()
        else:
            self.start()
        if not (self.snapd and self.snapd.alive()):
            self.snapd.start()
        wait_for(lambda: self.state() == "ready", 120, f"host {self.name} back to ready")

    def stop(self) -> None:
        if self.agent:
            self.thaw()
            self.agent.stop()
        if self.snapd:
            self.snapd.stop()

    def agent_vms(self) -> list[str]:
        """VM IDs the host agent itself has (asked over mTLS with the control-plane identity is not
        possible from here, so read its state store)."""
        import sqlite3

        db = sqlite3.connect(f"file:{self.root}/state/flint.db?mode=ro", uri=True)
        try:
            return [r[0] for r in db.execute("SELECT vm_id FROM sandboxes WHERE state != 'Dead'")]
        finally:
            db.close()


class Cluster:
    def __init__(self) -> None:
        self.port = int(os.environ.get("FLINT_CLUSTER_TEST_PORT", "9600"))
        self.base = os.environ.get("FLINT_CLUSTER_TEST_DIR", f"/srv/flint-cluster-test-{self.port}")
        self.backend = os.environ.get("FLINT_BACKEND", "firecracker")
        self.logs = os.path.join(self.base, "logs")
        self.binary = os.path.join(self.base, "bin", "flint-cluster")
        self.url = f"http://127.0.0.1:{self.port}"
        self.host_endpoint = f"https://127.0.0.1:{self.port + 1}"
        self.containers: list[str] = []
        self.servers: list[Proc] = []
        self.hosts: dict[str, Host] = {}
        self.lbs: list[Balancer] = []
        self.api = httpx.Client(base_url=self.url, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}, timeout=180)
        self.db_url = os.environ.get("FLINT_TEST_DATABASE_URL", "")
        self.store_url = os.environ.get("FLINT_TEST_STORE_URL", "")
        self.store_env: dict = {}

    # ── setup ──────────────────────────────────────────────────────────

    def build(self) -> None:
        if os.environ.get("FLINT_CLUSTER_BIN"):
            self.binary = os.environ["FLINT_CLUSTER_BIN"]  # prebuilt (e.g. by CI, or as a non-root user)
            return
        os.makedirs(os.path.dirname(self.binary), exist_ok=True)
        r = subprocess.run(["go", "build", "-o", self.binary, "./cmd/flint-cluster"], cwd=os.path.join(REPO, "cluster"),
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"go build failed:\n{r.stderr}")

    def _docker(self, name: str, args: list[str]) -> None:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        r = subprocess.run(["docker", "run", "-d", "--name", name, *args], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"docker run {name}: {r.stderr}")
        self.containers.append(name)

    def start_deps(self) -> None:
        if not self.db_url:
            self._docker(f"flint-test-pg-{self.port}", ["-e", "POSTGRES_PASSWORD=flint", "-e", "POSTGRES_USER=flint",
                         "-e", "POSTGRES_DB=flint", "-p", f"127.0.0.1:{self.port + 7}:5432", "postgres:16-alpine"])
            self.db_url = f"postgres://flint:flint@127.0.0.1:{self.port + 7}/flint?sslmode=disable"
        if not self.store_url:
            self._docker(f"flint-test-s3-{self.port}", ["-p", f"127.0.0.1:{self.port + 8}:8333",
                         "chrislusf/seaweedfs:latest", "server", "-s3", "-dir=/data",
                         "-master.volumeSizeLimitMB=512", "-volume.max=64"])
            endpoint = f"http://127.0.0.1:{self.port + 8}"
            wait_for(lambda: httpx.put(f"{endpoint}/flint-test").status_code in (200, 409), 60, "the S3 store")
            self.store_url = f"s3://flint-test/run-{int(time.time())}?endpoint={endpoint}"
        if self.store_url.startswith("s3://"):
            self.store_env = {"AWS_ACCESS_KEY_ID": os.environ.get("AWS_ACCESS_KEY_ID", "test"),
                              "AWS_SECRET_ACCESS_KEY": os.environ.get("AWS_SECRET_ACCESS_KEY", "test")}

    def server_proc(self, i: int) -> Proc:
        env = dict(os.environ, **self.store_env)
        return Proc(f"server-{i}", [
            self.binary, "server", "--database", self.db_url, "--store", self.store_url,
            "--listen", f"127.0.0.1:{self.port + 10 + i}", "--host-listen", f"127.0.0.1:{self.port + 20 + i}",
            "--host-endpoint", self.host_endpoint, "--admin-token", ADMIN_TOKEN, "--replica-id", f"replica-{i}",
            "--lease-ttl", "6s", "--fence-margin", "3s", "--checkpoint-interval", "20s",
        ], env, os.path.join(self.logs, f"server-{i}.log"))

    def start(self, hosts: list[tuple[str, str]]) -> None:
        global PIDS_DIR
        PIDS_DIR = os.path.join(self.base, "pids")
        kill_recorded(PIDS_DIR)
        os.makedirs(PIDS_DIR, exist_ok=True)
        # Hosts' identities belong to the previous run's control plane.
        shutil.rmtree(os.path.join(self.base, "hosts"), ignore_errors=True)
        shutil.rmtree(self.logs, ignore_errors=True)
        os.makedirs(self.logs, exist_ok=True)
        self.build()
        self.start_deps()
        self.lbs = [Balancer(self.port, [self.port + 10, self.port + 11]),
                    Balancer(self.port + 1, [self.port + 20, self.port + 21])]
        for i in range(2):
            p = self.server_proc(i)
            p.start()
            self.servers.append(p)
            wait_for(lambda: httpx.get(f"http://127.0.0.1:{self.port + 10 + i}/health").status_code == 200 or not p.alive(),
                     90, f"replica {i}")
            if not p.alive():
                raise RuntimeError(f"replica {i} exited:\n{tail(p.log)}")
        for idx, (name, zone) in enumerate(hosts, start=1):
            h = Host(self, name, idx, zone)
            self.hosts[name] = h  # before start, so teardown covers a host that failed to start
            h.start(self.join_token(zone))

    def join_token(self, zone: str = "") -> str:
        r = self.api.post("/v1/join-tokens", json={"zone": zone})
        r.raise_for_status()
        return r.json()["token"]

    def diagnostics(self) -> str:
        out = []
        for p in self.servers:
            out.append(f"--- {p.name} ---\n{tail(p.log, 25)}")
        for h in self.hosts.values():
            for p in (h.agent, h.snapd):
                if p:
                    out.append(f"--- {p.name} ---\n{tail(p.log, 25)}")
        return "\n".join(out)

    # ── teardown ───────────────────────────────────────────────────────

    def stop(self) -> None:
        try:
            for v in self.api.get("/vms").json().get("vms", []):
                self.api.delete(f"/vms/{v['vm_id']}")
            for vs in self.api.get("/v1/vmsets").json().get("vmsets", []):
                self.api.delete(f"/v1/vmsets/{vs['id']}")
        except Exception:
            pass
        for h in self.hosts.values():
            h.stop()
            h.crash()  # anything left in its cgroups
            self._cleanup_host_network(h)
            self._stop_nfs(h)
        for p in self.servers:
            p.stop()
        for lb in self.lbs:
            lb.close()
        for c in self.containers:
            with open(os.path.join(self.logs, f"{c}.log"), "wb") as f:
                subprocess.run(["docker", "logs", c], stdout=f, stderr=subprocess.STDOUT)
            subprocess.run(["docker", "rm", "-f", "-v", c], capture_output=True)

    @staticmethod
    def _cleanup_host_network(h: Host) -> None:
        subprocess.run(["ip", "link", "del", h.bridge], capture_output=True)
        for parent, chain in (("INPUT", f"FLINT-INPUT-{h.bridge}"[:28]), ("FORWARD", f"FLINT-FORWARD-{h.bridge}"[:28])):
            subprocess.run(["iptables", "-D", parent, "-j", chain], capture_output=True)
            subprocess.run(["iptables", "-F", chain], capture_output=True)
            subprocess.run(["iptables", "-X", chain], capture_output=True)
        subprocess.run(["iptables", "-t", "nat", "-D", "POSTROUTING", "-s", f"{h.subnet}.0/24", "-j", "MASQUERADE"], capture_output=True)

    @staticmethod
    def _stop_nfs(h: Host) -> None:
        pid_path = os.path.join(h.root, "state", "r2nfs.pid")
        try:
            with open(pid_path) as f:
                os.kill(int(f.read().strip()), signal.SIGTERM)
        except (OSError, ValueError):
            pass


def host_client_ctx(host: Host) -> ssl.SSLContext:
    """TLS context carrying a host's certificate (to check what it can't do)."""
    ctx = ssl.create_default_context(cafile=os.path.join(host.identity, "cp-ca.pem"))
    ctx.load_cert_chain(os.path.join(host.identity, "host.crt"), os.path.join(host.identity, "host.key"))
    return ctx


def raw_join(cluster: Cluster, token: str) -> int:
    """POST a join with a throwaway CSR; returns the HTTP status."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    csr = x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "intruder")])).sign(key, hashes.SHA256())
    body = json.dumps({"token": token, "name": "intruder", "csr_pem": csr.public_bytes(serialization.Encoding.PEM).decode(),
                       "agent_url": "https://127.0.0.1:1", "snapd_url": "https://127.0.0.1:2"}).encode()
    req = urllib.request.Request(cluster.host_endpoint + "/v1/join", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def have_prereqs() -> str | None:
    if os.geteuid() != 0:
        return "cluster tests need root"
    if not os.path.exists("/dev/kvm"):
        return "no /dev/kvm"
    if not shutil.which("go"):
        return "go not installed"
    if not (os.environ.get("FLINT_TEST_DATABASE_URL") and os.environ.get("FLINT_TEST_STORE_URL")) and not shutil.which("docker"):
        return "docker not available (or set FLINT_TEST_DATABASE_URL and FLINT_TEST_STORE_URL)"
    return None


if __name__ == "__main__":
    # Bring up a cluster to use by hand (or to run tests/ against), until Ctrl-C:
    #   sudo -E .venv/bin/python -m tests.cluster.harness
    reason = have_prereqs()
    if reason:
        sys.exit(f"can't run a cluster here: {reason}")
    c = Cluster()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        c.start([("a", "zone-1"), ("b", "zone-2")])
        print(f"export FLINT_URL={c.url} FLINT_API_KEY={ADMIN_TOKEN} FLINT_TENANT=dev", flush=True)
        print(f"logs: {c.logs}", flush=True)
        stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        c.stop()
