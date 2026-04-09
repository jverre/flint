#!/bin/bash
set -euo pipefail

ROOTFS_DIR="/root/firecracker-vm"
ROOTFS_IMG="${ROOTFS_DIR}/rootfs.ext4"
ALPINE_TAR="${ROOTFS_DIR}/alpine-minirootfs-3.21.3-x86_64.tar.gz"
MOUNT_POINT="/tmp/firecracker-rootfs"

# Cleanup any previous state
sudo umount "$MOUNT_POINT" 2>/dev/null || true
rm -f "$ROOTFS_IMG"

# Create fresh ext4 image
truncate -s 200M "$ROOTFS_IMG"
mkfs.ext4 -q "$ROOTFS_IMG"

# Mount and populate
mkdir -p "$MOUNT_POINT"
sudo mount "$ROOTFS_IMG" "$MOUNT_POINT"

# Ensure cleanup on exit
trap 'sudo umount "$MOUNT_POINT" 2>/dev/null || true' EXIT

# Extract Alpine rootfs
sudo tar xzf "$ALPINE_TAR" -C "$MOUNT_POINT"

# Build flintd guest agent (static Go binary)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/guest/flintd" && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -buildvcs=false -ldflags="-s -w" -o /tmp/flintd .
cd "$SCRIPT_DIR"
sudo cp /tmp/flintd "$MOUNT_POINT/usr/local/bin/flintd"
sudo chmod +x "$MOUNT_POINT/usr/local/bin/flintd"

# Write network init script (TAP + TCP replaces vsock)
sudo tee "$MOUNT_POINT/etc/init-net.sh" > /dev/null << 'INITSCRIPT'
#!/bin/sh
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev 2>/dev/null
mkdir -p /dev/pts
mount -t devpts devpts /dev/pts

# Configure network
ip addr add 172.16.0.2/30 dev eth0
ip link set eth0 up
ip route add default via 172.16.0.1

# DNS
echo "nameserver 8.8.8.8" > /etc/resolv.conf

# Ensure workspace directory exists for storage backends
mkdir -p /workspace

# Start flintd guest agent (HTTP+WebSocket process manager)
/usr/local/bin/flintd > /var/log/flintd.log 2>&1 &

# Wait for flintd to be listening before signaling READY
i=0
while [ "$i" -lt 200 ]; do
    if grep -q ":1388" /proc/net/tcp 2>/dev/null; then
        break
    fi
    i=$((i + 1))
    sleep 0.05 2>/dev/null || sleep 1
done

echo "READY"
exec /bin/sh
INITSCRIPT

sudo sed -i 's/\r$//' "$MOUNT_POINT/etc/init-net.sh"
sudo chmod +x "$MOUNT_POINT/etc/init-net.sh"

# Verify
echo "--- Verifying init script ---"
sudo ls -la "$MOUNT_POINT/etc/init-net.sh"
sudo head -1 "$MOUNT_POINT/etc/init-net.sh" | cat -A

echo "rootfs ready at $ROOTFS_IMG"