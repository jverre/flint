#!/usr/local/lib/flint/busybox sh
# PID 1 of a VM booted from an imported template. Boot steps use Flint's static
# busybox, so they work whatever the image ships (distroless included).
bb=/usr/local/lib/flint/busybox

# The kernel starts init with no PATH. Use the image's own environment (as
# `docker run` would), then fall back to the bundled tools for anything missing.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
[ -f /etc/flint/image.env ] && . /etc/flint/image.env
export PATH="$PATH:/usr/local/lib/flint/bin"

$bb mkdir -p /proc /sys /dev /tmp /var/log /workspace
$bb mount -t proc proc /proc
$bb mount -t sysfs sys /sys
$bb mount -t devtmpfs dev /dev 2>/dev/null
$bb mkdir -p /dev/pts
$bb mount -t devpts devpts /dev/pts

# Configure network (loopback too: programs expect localhost to work)
$bb ip link set lo up
$bb ip addr add 172.16.0.2/30 dev eth0
$bb ip link set eth0 up
$bb ip route add default via 172.16.0.1

# DNS. Images often ship resolv.conf as a symlink into a runtime directory.
$bb rm -f /etc/resolv.conf
echo "nameserver 8.8.8.8" > /etc/resolv.conf

# Start flintd guest agent (HTTP+WebSocket process manager)
/usr/local/bin/flintd > /var/log/flintd.log 2>&1 &

# Wait for flintd to be listening before signaling READY
i=0
while [ "$i" -lt 200 ]; do
    # :1388 is port 5000; a dual-stack listener only shows in tcp6.
    if $bb grep -qs ":1388" /proc/net/tcp /proc/net/tcp6; then
        break
    fi
    i=$((i + 1))
    $bb sleep 0.05
done

echo "READY"
exec $bb sh
