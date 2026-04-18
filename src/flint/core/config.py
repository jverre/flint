import logging
import os
import platform

log = logging.getLogger(__name__)


def _default_dirs() -> tuple[str, str]:
    system = platform.system()
    if system == "Darwin":
        base = os.path.expanduser("~/Library/Application Support/flint")
        return f"{base}/data", f"{base}/state"
    return "/microvms", "/tmp/flint"

# ── Env-var-driven directories ────────────────────────────────────────────
_DEFAULT_DATA_DIR, _DEFAULT_STATE_DIR = _default_dirs()
DAEMON_PORT = int(os.environ.get("FLINT_PORT", "9100"))
DATA_DIR = os.environ.get("FLINT_DATA_DIR", _DEFAULT_DATA_DIR)
STATE_DIR = os.environ.get("FLINT_STATE_DIR", _DEFAULT_STATE_DIR)

# Debug log to file
os.makedirs(STATE_DIR, exist_ok=True)
_log_handler = logging.FileHandler(f"{STATE_DIR}/flint-debug.log")
_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
log.addHandler(_log_handler)
log.setLevel(logging.DEBUG)

# ── Constants ────────────────────────────────────────────────────────────────

SOURCE_ROOTFS = "/root/firecracker-vm/rootfs.ext4"
KERNEL_PATH = "/root/firecracker-vm/vmlinux"
BOOT_ARGS = "console=ttyS0 quiet loglevel=0 reboot=k panic=1 pci=off random.trust_cpu=on mitigations=off nokaslr raid=noautodetect init=/etc/init-net.sh"
GUEST_MAC = "06:00:AC:10:00:02"
HOST_TAP_MAC = "06:00:AC:10:00:01"

AGENT_PORT = 5000
GUEST_IP = "172.16.0.2"
HOST_IP = "172.16.0.1"

BRIDGE_NAME = "br-flint"
BRIDGE_IP = "10.0.0.1"
BRIDGE_CIDR = 24
VETH_SUBNET = "10.0.0"  # VMs get 10.0.0.{2..254}

GOLDEN_TAP = "tap-golden"
GOLDEN_NS = "fc-golden"
GOLDEN_DIR = f"{DATA_DIR}/.golden"

POOL_DIR = f"{DATA_DIR}/.pool"
POOL_TARGET_SIZE = 8
POOL_WORKERS = 4

TEMPLATES_DIR = f"{DATA_DIR}/.templates"
DEFAULT_TEMPLATE_ID = "default"

TERM_COLS = 120
TERM_ROWS = 40

# ── Daemon ────────────────────────────────────────────────────────────────
DAEMON_HOST = "127.0.0.1"
DAEMON_URL = f"http://{DAEMON_HOST}:{DAEMON_PORT}"
DAEMON_DIR = STATE_DIR
DAEMON_STATE_PATH = f"{STATE_DIR}/state.json"
DAEMON_PID_PATH = f"{STATE_DIR}/flintd.pid"
DAEMON_DB_PATH = f"{STATE_DIR}/flint.db"

# ── Jailer ───────────────────────────────────────────────────────────────────
JAILER_BINARY      = os.environ.get("FLINT_JAILER_BINARY", "jailer")
FIRECRACKER_BINARY = os.environ.get("FLINT_FIRECRACKER_BINARY", "/usr/local/bin/firecracker")
JAILER_BASE_DIR    = os.environ.get("FLINT_JAILER_BASE_DIR", "/srv/jailer")
JAILER_UID         = int(os.environ.get("FLINT_JAILER_UID", "1000"))
JAILER_GID         = int(os.environ.get("FLINT_JAILER_GID", "1000"))
JAILER_CGROUP_VER  = int(os.environ.get("FLINT_JAILER_CGROUP_VER", "2"))

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
CH_SLICE          = os.environ.get("FLINT_CH_SLICE", "flint.slice")
CH_UID            = int(os.environ.get("FLINT_CH_UID", str(JAILER_UID)))

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
