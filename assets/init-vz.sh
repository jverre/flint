#!/bin/sh
set -eu

mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev 2>/dev/null || true
mkdir -p /dev/pts
mount -t devpts devpts /dev/pts 2>/dev/null || true

# Auto-detect the first non-loopback network interface.
# VZ may name it eth0 or enp0s1 depending on the kernel.
IFACE=""
for candidate in eth0 enp0s1 enp0s2 enp0s5; do
    if [ -d "/sys/class/net/$candidate" ]; then
        IFACE="$candidate"
        break
    fi
done
if [ -z "$IFACE" ]; then
    # Fallback: first non-lo interface
    for d in /sys/class/net/*; do
        name="$(basename "$d")"
        [ "$name" = "lo" ] && continue
        IFACE="$name"
        break
    done
fi

if [ -n "$IFACE" ]; then
    ip link set "$IFACE" up 2>/dev/null || true
    if command -v udhcpc >/dev/null 2>&1; then
        udhcpc -i "$IFACE" -q -n -T 3 >/tmp/udhcpc.log 2>&1 || cat /tmp/udhcpc.log >&2
    fi
fi

IP=""
if [ -n "$IFACE" ]; then
    if command -v ip >/dev/null 2>&1; then
        IP="$(ip -4 addr show dev "$IFACE" | awk '/inet / {print $2}' | cut -d/ -f1 | head -n1)" || true
    else
        IP="$(ifconfig "$IFACE" 2>/dev/null | awk '/inet / {print $2} /inet addr:/ {sub("addr:", "", $2); print $2}' | head -n1)" || true
    fi
fi

if [ -n "${IP:-}" ]; then
    echo "FLINT_IP=$IP"
fi

/usr/local/bin/flintd > /var/log/flintd.log 2>&1 &

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
