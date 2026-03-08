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

# Setup DNS for chroot and install socat
sudo cp /etc/resolv.conf "$MOUNT_POINT/etc/resolv.conf"
sudo chroot "$MOUNT_POINT" /bin/sh -c "apk add --no-cache socat"

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

# Start TCP shell server
socat TCP-LISTEN:5000,bind=0.0.0.0,reuseaddr,fork EXEC:"/bin/sh -i",pty,stderr,setsid,sigint,sane &

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