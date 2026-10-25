#!/usr/bin/env bash
# Install a Flint cluster node on Ubuntu/Debian. Idempotent.
#
#   FLINT_ROLE=server|host   what this machine runs (required)
#   FLINT_REF=main           git ref of github.com/jverre/flint to build
#   FLINT_REPO=https://github.com/jverre/flint
#   FLINT_BACKEND=firecracker|cloud-hypervisor   (hosts)
#
# Configuration lives in /etc/flint/flint.env (see deploy/flint.env.example);
# write it before running this, then this enables and starts the units.
set -euo pipefail

ROLE="${FLINT_ROLE:?set FLINT_ROLE=server or FLINT_ROLE=host}"
REF="${FLINT_REF:-main}"
REPO="${FLINT_REPO:-https://github.com/jverre/flint}"
BACKEND="${FLINT_BACKEND:-firecracker}"
GO_VERSION="${GO_VERSION:-1.26.0}"
SRC=/opt/flint/src

log() { printf '[flint-install] %s\n' "$*"; }

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
case "$ROLE" in server|host) ;; *) echo "FLINT_ROLE must be server or host" >&2; exit 1 ;; esac

if [ "$ROLE" = host ]; then
  : "${FLINT_HOST_KERNEL:?pin the exact approved host kernel release}"
  : "${FLINT_HYPERVISOR_VERSION:?pin the approved hypervisor release}"
  [ "$(uname -r)" = "$FLINT_HOST_KERNEL" ] || { echo "running host kernel differs from approved image" >&2; exit 1; }
  [[ "$FLINT_HYPERVISOR_VERSION" =~ ^v?[0-9]+\.[0-9]+(\.[0-9]+)?$ ]] || { echo "hypervisor release must be pinned" >&2; exit 1; }
fi

log "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl ca-certificates python3 nftables iproute2 e2fsprogs util-linux >/dev/null

if ! command -v go >/dev/null || [ "$(go env GOVERSION 2>/dev/null)" != "go${GO_VERSION}" ]; then
  log "go ${GO_VERSION}"
  arch=$(dpkg --print-architecture)
  curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-${arch}.tar.gz" | tar -C /usr/local -xz
  ln -sf /usr/local/go/bin/go /usr/local/bin/go
fi
export GOTOOLCHAIN=local GOPATH=/opt/flint/go GOCACHE=/opt/flint/go-cache

log "source ${REPO}@${REF}"
if [ -d "$SRC/.git" ]; then
  git -C "$SRC" fetch -q --depth 1 origin "$REF" || { echo "fetching $REF failed" >&2; exit 1; }
  git -C "$SRC" checkout -q FETCH_HEAD
else
  mkdir -p "$(dirname "$SRC")"
  # A branch or tag clones shallow; a commit SHA needs the full history.
  if ! git clone -q --depth 1 --branch "$REF" "$REPO" "$SRC" 2>/dev/null; then
    rm -rf "$SRC"
    git clone -q "$REPO" "$SRC" || { echo "cloning $REPO failed" >&2; exit 1; }
    git -C "$SRC" checkout -q "$REF" || { echo "no ref $REF in $REPO" >&2; exit 1; }
  fi
fi

# Runtime code and its environment never change beneath a running agent.
COMMIT=$(git -C "$SRC" rev-parse HEAD)
RELEASE="/opt/flint/releases/$COMMIT"
if [ ! -d "$RELEASE/src" ]; then
  mkdir -p "$RELEASE"
  stage="$RELEASE/src.tmp-$$"
  mkdir "$stage"
  git -C "$SRC" archive "$COMMIT" | tar -x -C "$stage"
  mv "$stage" "$RELEASE/src"
fi
SRC="$RELEASE/src"

log "flint-cluster"
(cd "$SRC/cluster" && go build -trimpath -o /usr/local/bin/.flint-cluster-new ./cmd/flint-cluster)
mv -Tf /usr/local/bin/.flint-cluster-new /usr/local/bin/flint-cluster

mkdir -p /etc/flint
[ -f /etc/flint/flint.env ] || cp "$SRC/deploy/flint.env.example" /etc/flint/flint.env
chmod 600 /etc/flint/flint.env
cp "$SRC"/deploy/systemd/*.service "$SRC"/deploy/systemd/*.path /etc/systemd/system/

if [ "$ROLE" = server ]; then
  id flint >/dev/null 2>&1 || useradd --system --home /var/lib/flint --shell /usr/sbin/nologin flint
  chgrp flint /etc/flint/flint.env && chmod 640 /etc/flint/flint.env
  for f in /etc/flint/*.key; do [ -e "$f" ] && chgrp flint "$f" && chmod 640 "$f"; done
  systemctl daemon-reload
  systemctl enable --now flint-server.service
  systemctl try-restart flint-server.service
  log "control plane started: journalctl -u flint-server"
  exit 0
fi

# ── host ──────────────────────────────────────────────────────────────────
[ -e /dev/kvm ] || { echo "no /dev/kvm: hosts need bare metal or nested virtualization" >&2; exit 1; }

if ! command -v uv >/dev/null; then
  log "uv"
  curl -fsSL https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh >/dev/null
fi
log "flint (python)"
(cd "$SRC" && UV_PROJECT_ENVIRONMENT="$RELEASE/venv" uv sync -q --no-dev --frozen)

log "r2nfs (volumes)"
(cd "$SRC/services/r2nfs" && go build -trimpath -o r2nfs .)

log "hypervisor and guest kernel"
if [ "$BACKEND" = cloud-hypervisor ]; then
  "$RELEASE/venv/bin/flint" install-deps --backend cloud-hypervisor --version "$FLINT_HYPERVISOR_VERSION"
else
  FC_VERSION="$FLINT_HYPERVISOR_VERSION" INSTALL_DIR=/usr/local/bin KERNEL_DIR=/root/firecracker-vm bash "$SRC/scripts/install-deps.sh"
fi

if [ ! -f /root/firecracker-vm/rootfs.ext4 ]; then
  log "guest rootfs"
  tarball=/root/firecracker-vm/alpine-minirootfs-3.21.3-x86_64.tar.gz
  [ -f "$tarball" ] || curl -fsSL -o "$tarball" \
    https://dl-cdn.alpinelinux.org/alpine/v3.21/releases/x86_64/alpine-minirootfs-3.21.3-x86_64.tar.gz
  (cd "$SRC" && bash setup-rootfs.sh)
fi

mkdir -p /var/lib/flint/data /var/lib/flint/state /var/lib/flint/snapshot-cache /srv/jailer
# The watchdog backs fencing (plans/03-cluster.md); softdog where there's no hardware one.
if grep -q '^FLINT_WATCHDOG_DEVICE=/dev/watchdog' /etc/flint/flint.env && [ ! -e /dev/watchdog ]; then
  modprobe softdog && echo softdog > /etc/modules-load.d/flint-softdog.conf
fi

systemctl daemon-reload
# Prepare the upgrade while the old agent still renews normally. A missing
# snapd handover API or active VM operation aborts before stopping the agent.
if systemctl is-active --quiet flint-agent.service; then
  "$RELEASE/venv/bin/flint" agent-handover || {
    echo "handover refused; old agent left running. Settle VM operations and upgrade snapd separately before retrying." >&2
    exit 1
  }
fi
ln -s "$RELEASE/venv/bin/flint" /usr/local/bin/.flint-new-$$
mv -Tf /usr/local/bin/.flint-new-$$ /usr/local/bin/flint
systemctl enable --now flint-snapd.path
if systemctl is-active --quiet flint-agent.service; then
  systemctl restart flint-agent.service
else
  systemctl enable --now flint-agent.service
fi
log "host agent started: journalctl -u flint-agent -u flint-snapd"
