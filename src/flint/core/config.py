import hashlib
import json
import logging
import os

log = logging.getLogger(__name__)


# Where the daemon is (shared with its clients, which must not import core).
from flint._settings import (  # noqa: E402,F401
    API_KEY, CREDENTIALS_PATH, DAEMON_HOST, DAEMON_PORT, DAEMON_SOCKET, DAEMON_URL, DEFAULT_TEMPLATE_ID,
    STATE_DIR, TENANT, TERM_COLS, TERM_ROWS, default_dirs, stored_token,
)

# ── Env-var-driven directories ────────────────────────────────────────────
DATA_DIR = os.environ.get("FLINT_DATA_DIR", default_dirs()[0])

# Debug log to file
os.makedirs(STATE_DIR, exist_ok=True)
_log_handler = logging.FileHandler(f"{STATE_DIR}/flint-debug.log")
_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
log.addHandler(_log_handler)
log.setLevel(logging.DEBUG)

# ── Constants ────────────────────────────────────────────────────────────────

SOURCE_ROOTFS = os.environ.get("FLINT_ROOTFS_PATH", "/root/firecracker-vm/rootfs.ext4")
KERNEL_PATH = os.environ.get("FLINT_KERNEL_PATH", "/root/firecracker-vm/vmlinux")
BOOT_ARGS = "console=ttyS0 quiet loglevel=0 reboot=k panic=1 pci=off random.trust_cpu=on mitigations=off nokaslr raid=noautodetect init=/etc/init-net.sh"
GUEST_MAC = "06:00:AC:10:00:02"
HOST_TAP_MAC = "06:00:AC:10:00:01"

AGENT_PORT = 5000
GUEST_IP = "172.16.0.2"
HOST_IP = "172.16.0.1"

# One bridge + /24 per daemon. Override both to run several daemons (e.g. two
# cluster host agents) on one machine without sharing addresses or firewall chains.
BRIDGE_NAME = os.environ.get("FLINT_BRIDGE_NAME", "br-flint")
VETH_SUBNET = os.environ.get("FLINT_BRIDGE_SUBNET", "10.0.0")  # VMs get <subnet>.{2..254}
BRIDGE_IP = f"{VETH_SUBNET}.1"
BRIDGE_CIDR = 24
# Host chains for this bridge. iptables chain names are at most 28 characters.
_CHAIN_SUFFIX = "" if BRIDGE_NAME == "br-flint" else f"-{BRIDGE_NAME}"
HOST_INPUT_CHAIN = f"FLINT-INPUT{_CHAIN_SUFFIX}"[:28]
HOST_FORWARD_CHAIN = f"FLINT-FORWARD{_CHAIN_SUFFIX}"[:28]

GOLDEN_TAP = "tap-golden"
GOLDEN_DIR = f"{DATA_DIR}/.golden"

POOL_DIR = f"{DATA_DIR}/.pool"
POOL_TARGET_SIZE = 8
POOL_WORKERS = 4
# Pre-built network namespaces with internet access. Each holds one of the
# bridge's 253 IPs, which also caps concurrent internet-enabled sandboxes.
NETNS_POOL_SIZE = int(os.environ.get("FLINT_NETNS_POOL_SIZE", "8"))

TEMPLATES_DIR = f"{DATA_DIR}/.templates"

# ── Daemon ────────────────────────────────────────────────────────────────
# Written at the daemon's first start: an admin token for TCP clients (the web UI).
ADMIN_TOKEN_PATH = f"{STATE_DIR}/admin.token"
DAEMON_DIR = STATE_DIR
DAEMON_STATE_PATH = f"{STATE_DIR}/state.json"
DAEMON_PID_PATH = f"{STATE_DIR}/flintd.pid"
DAEMON_DB_PATH = f"{STATE_DIR}/flint.db"

# ── Jailer ───────────────────────────────────────────────────────────────────
JAILER_BINARY      = os.environ.get("FLINT_JAILER_BINARY", "jailer")
FIRECRACKER_BINARY = os.environ.get("FLINT_FIRECRACKER_BINARY", "/usr/local/bin/firecracker")
JAILER_BASE_DIR    = os.environ.get("FLINT_JAILER_BASE_DIR", "/srv/jailer")
# Host process identities are allocated per resource by _identities.py.
JAILER_CGROUP_VER  = int(os.environ.get("FLINT_JAILER_CGROUP_VER", "2"))
# Pool entries and shared read-only artifacts (snapshots, kernel, VMM binary)
# live on the jailer's filesystem so they can be hard-linked into each chroot.
# Pools are per daemon (keyed by data dir); shared artifacts are keyed by source.
_DATA_KEY          = hashlib.sha1(DATA_DIR.encode()).hexdigest()[:8]
GOLDEN_NS          = f"fc-golden-{_DATA_KEY}"
# cgroup v2 base of every jailed Firecracker VM; the jailer joins its VM leaf.
JAILER_PARENT_CGROUP = os.environ.get("FLINT_JAILER_PARENT_CGROUP", "firecracker")
FC_POOL_DIR        = f"{JAILER_BASE_DIR}/.pool-fc-{_DATA_KEY}"
CH_POOL_DIR        = f"{JAILER_BASE_DIR}/.pool-ch-{_DATA_KEY}"
JAILER_SHARED_DIR  = f"{JAILER_BASE_DIR}/.snapshots"

# ── Credential proxy ─────────────────────────────────────────────────────
PROXY_PORT = int(os.environ.get("FLINT_PROXY_PORT", "8080"))
PROXY_CA_DIR = f"{STATE_DIR}/proxy-ca"

# ── State management ─────────────────────────────────────────────────────
HEALTH_CHECK_INTERVAL = 5.0         # seconds between health probes
DEFAULT_SANDBOX_TIMEOUT = 300       # seconds before auto-cleanup (5 min)
ERROR_CLEANUP_DELAY = 60            # seconds to keep error-state sandboxes before cleanup

# ── Cloud-Hypervisor backend ─────────────────────────────────────────────
CH_BINARY         = os.environ.get("FLINT_CH_BINARY", "/usr/local/bin/cloud-hypervisor")
CH_CPU_COUNT      = int(os.environ.get("FLINT_CH_CPU_COUNT", "1"))
CH_MEMORY_BYTES   = int(os.environ.get("FLINT_CH_MEMORY_BYTES", str(128 * 1024 * 1024)))
CH_GOLDEN_DIR     = os.environ.get("FLINT_CH_GOLDEN_DIR", f"{DATA_DIR}/.golden-ch")
# cgroup v2 base of every jailed cloud-hypervisor VMM (cf. JAILER_PARENT_CGROUP).
CH_PARENT_CGROUP  = os.environ.get("FLINT_CH_PARENT_CGROUP", "cloud-hypervisor")

# ── macOS Virtualization.framework backend ───────────────────────────────
VZ_KERNEL_PATH = os.environ.get("FLINT_VZ_KERNEL_PATH", os.path.join(DATA_DIR, "vz", "vmlinux"))
VZ_ROOTFS_PATH = os.environ.get("FLINT_VZ_ROOTFS_PATH", os.path.join(DATA_DIR, "vz", "rootfs.img"))
VZ_INITRD_PATH = os.environ.get("FLINT_VZ_INITRD_PATH", "")
VZ_BOOT_ARGS = os.environ.get("FLINT_VZ_BOOT_ARGS", "console=hvc0 root=/dev/vda rw init=/etc/init-net.sh net.ifnames=0")
VZ_CPU_COUNT = int(os.environ.get("FLINT_VZ_CPU_COUNT", "2"))
VZ_MEMORY_BYTES = int(os.environ.get("FLINT_VZ_MEMORY_BYTES", str(2 * 1024 * 1024 * 1024)))
VZ_READY_TIMEOUT = float(os.environ.get("FLINT_VZ_READY_TIMEOUT", "60"))

# ── Storage backend ─────────────────────────────────────────────────────
STORAGE_BACKEND = os.environ.get("FLINT_STORAGE_BACKEND", "local")  # "local" | "s3_files" | "r2"
WORKSPACE_DIR = os.environ.get("FLINT_WORKSPACE_DIR", "/workspace")

# ── Cluster host agent (`flint agent`) ─────────────────────────────────
# In a cluster this daemon is a host agent: the control plane drives it over
# mTLS and it reports to the control plane. See plans/03-cluster.md.
CLUSTER_MODE = os.environ.get("FLINT_CLUSTER") == "1"
CLUSTER_IDENTITY_DIR = os.environ.get("FLINT_IDENTITY_DIR", f"{STATE_DIR}/cluster")
SNAPD_SOCKET = os.environ.get("FLINT_SNAPD_SOCKET", "/run/flint/snapd.sock")
# Most VMs (running, not paused) this host takes; 0 = derive from memory.
MAX_VMS = int(os.environ.get("FLINT_MAX_VMS", "0"))
# A cluster-wide directory of root disks (e.g. on NFS). When set, Cloud
# Hypervisor VMs boot from a disk there on a migration network, so they can
# move between hosts live. See _shared_rootfs.py and net/migration_network.py.
SHARED_ROOTFS_DIR = os.environ.get("FLINT_SHARED_ROOTFS_DIR", "")

# ── Runtime-editable overrides ──────────────────────────────────────────
CONFIG_OVERRIDES_PATH = f"{STATE_DIR}/config.json"
CONFIG_FIELDS = {
    "pool_target_size": ("POOL_TARGET_SIZE", int),
    "default_sandbox_timeout": ("DEFAULT_SANDBOX_TIMEOUT", int),
    "health_check_interval": ("HEALTH_CHECK_INTERVAL", float),
    "error_cleanup_delay": ("ERROR_CLEANUP_DELAY", int),
}


def _apply_config_overrides() -> None:
    """Load overrides from CONFIG_OVERRIDES_PATH and patch module globals."""
    try:
        with open(CONFIG_OVERRIDES_PATH) as f:
            overrides = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    g = globals()
    for key, value in overrides.items():
        meta = CONFIG_FIELDS.get(key)
        if not meta:
            continue
        const_name, typ = meta
        try:
            g[const_name] = typ(value)
        except (TypeError, ValueError):
            log.warning("Invalid override for %s: %r", key, value)


_apply_config_overrides()

# S3 Files backend
S3_FILES_NFS_ENDPOINT = os.environ.get("FLINT_S3_FILES_NFS_ENDPOINT", "")

# R2 backend
R2_ACCOUNT_ID = os.environ.get("FLINT_R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("FLINT_R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("FLINT_R2_SECRET_ACCESS_KEY", "")
R2_BUCKET = os.environ.get("FLINT_R2_BUCKET", "flint-storage")
R2_CACHE_DIR = os.environ.get("FLINT_R2_CACHE_DIR", os.path.join(STATE_DIR, "r2-cache"))
R2_CACHE_SIZE_MB = int(os.environ.get("FLINT_R2_CACHE_SIZE_MB", "1024"))
R2NFS_PORT = int(os.environ.get("FLINT_R2NFS_PORT", "2049"))
R2NFS_MGMT_PORT = int(os.environ.get("FLINT_R2NFS_MGMT_PORT", "9200"))
R2NFS_MGMT_SOCKET = os.path.join(STATE_DIR, "r2nfs.sock")
# Sandboxes attach exclusive volumes (read-write) and snapshots of them
# (read-only) over NBD from the same server.
NBD_PORT = int(os.environ.get("FLINT_NBD_PORT", "10809"))
NBD_RO_PORT = int(os.environ.get("FLINT_NBD_RO_PORT", "10810"))
