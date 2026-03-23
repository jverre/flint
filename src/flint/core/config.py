import logging
import os

log = logging.getLogger(__name__)

# ── Env-var-driven directories ────────────────────────────────────────────
DAEMON_PORT = int(os.environ.get("FLINT_PORT", "9100"))
DATA_DIR = os.environ.get("FLINT_DATA_DIR", "/microvms")
STATE_DIR = os.environ.get("FLINT_STATE_DIR", "/tmp/flint")

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
PROXY_PORT = 8080
PROXY_CA_DIR = f"{STATE_DIR}/proxy-ca"

# ── State management ─────────────────────────────────────────────────────
HEALTH_CHECK_INTERVAL = 5.0         # seconds between health probes
DEFAULT_SANDBOX_TIMEOUT = 300       # seconds before auto-cleanup (5 min)
ERROR_CLEANUP_DELAY = 60            # seconds to keep error-state sandboxes before cleanup
