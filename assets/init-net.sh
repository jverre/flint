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
