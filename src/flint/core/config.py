import logging

log = logging.getLogger(__name__)

# Debug log to file — tail -f /tmp/flint-debug.log
_log_handler = logging.FileHandler("/tmp/flint-debug.log")
_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
log.addHandler(_log_handler)
log.setLevel(logging.DEBUG)

# ── Constants ────────────────────────────────────────────────────────────────

SOURCE_ROOTFS = "/root/firecracker-vm/rootfs.ext4"
KERNEL_PATH = "/root/firecracker-vm/vmlinux"
BOOT_ARGS = "console=ttyS0 quiet loglevel=0 reboot=k panic=1 pci=off random.trust_cpu=on mitigations=off nokaslr raid=noautodetect init=/etc/init-net.sh"
GUEST_MAC = "06:00:AC:10:00:02"
HOST_TAP_MAC = "06:00:AC:10:00:01"

TCP_PORT = 5000
GUEST_IP = "172.16.0.2"
HOST_IP = "172.16.0.1"

GOLDEN_TAP = "tap-golden"
GOLDEN_NS = "fc-golden"
GOLDEN_DIR = "/microvms/.golden"

POOL_DIR = "/microvms/.pool"
POOL_TARGET_SIZE = 8
POOL_WORKERS = 4

TERM_COLS = 120
TERM_ROWS = 40

# ── Daemon ────────────────────────────────────────────────────────────────
DAEMON_HOST = "127.0.0.1"
DAEMON_PORT = 9100
DAEMON_URL = f"http://{DAEMON_HOST}:{DAEMON_PORT}"
DAEMON_DIR = "/tmp/flint"
DAEMON_STATE_PATH = "/tmp/flint/state.json"
DAEMON_PID_PATH = "/tmp/flint/flintd.pid"
