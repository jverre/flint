#!/usr/bin/env bash
set -euo pipefail

# Firecracker + jailer + vmlinux installer
# Usage: curl -fsSL https://raw.githubusercontent.com/jverre/flint/main/scripts/install-deps.sh | sudo bash
# Env vars:
#   FC_VERSION    - firecracker version to install (default: latest)
#   INSTALL_DIR   - where to install binaries (default: /usr/local/bin)
#   KERNEL_DIR    - where to store vmlinux (default: /root/firecracker-vm)
#   SKIP_KERNEL   - set to 1 to skip vmlinux download (default: 0)
#   KERNEL_RELEASE - Flint guest kernel release tag (default below; built by scripts/build-kernel.sh)

FC_VERSION="${FC_VERSION:-latest}"
INSTALL_DIR="${INSTALL_DIR:-/usr/local/bin}"
KERNEL_DIR="${KERNEL_DIR:-/root/firecracker-vm}"
SKIP_KERNEL="${SKIP_KERNEL:-0}"
KERNEL_RELEASE="${KERNEL_RELEASE:-guest-kernel-6.1.141-flint2}"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

info()  { printf "  [+] %s\n" "$*"; }
skip()  { printf "  [!] %s\n" "$*"; }
error() { printf "  [x] %s\n" "$*" >&2; exit 1; }

# Detect architecture
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|aarch64) ;;
  *) error "Unsupported architecture: $ARCH (supported: x86_64, aarch64)" ;;
esac

# Resolve latest version if needed
if [ "$FC_VERSION" = "latest" ]; then
  info "Resolving latest Firecracker version..."
  FC_VERSION="$(basename "$(curl -fsSLI -o /dev/null -w '%{url_effective}' \
    https://github.com/firecracker-microvm/firecracker/releases/latest)")"
  info "Latest version: $FC_VERSION"
fi

# Check if already at target version
if command -v firecracker >/dev/null 2>&1; then
  INSTALLED="$(firecracker --version 2>/dev/null | awk '{print $NF}' | head -1 || true)"
  if [ "$INSTALLED" = "$FC_VERSION" ]; then
    skip "firecracker $FC_VERSION already installed - skipping download"
    SKIP_FC=1
  else
    SKIP_FC=0
  fi
else
  SKIP_FC=0
fi

if [ "${SKIP_FC}" = "0" ]; then
  TARBALL="firecracker-${FC_VERSION}-${ARCH}.tgz"
  CHECKSUM_FILE="${TARBALL}.sha256.txt"
  RELEASE_BASE="https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VERSION}"

  info "Downloading firecracker $FC_VERSION for $ARCH..."
  curl -fsSL -o "$TMPDIR/$TARBALL" "$RELEASE_BASE/$TARBALL"
  curl -fsSL -o "$TMPDIR/$CHECKSUM_FILE" "$RELEASE_BASE/$CHECKSUM_FILE"

  info "Verifying SHA256 checksum..."
  cd "$TMPDIR"
  sha256sum -c "$CHECKSUM_FILE" --status || error "Checksum verification failed"
  cd - >/dev/null
  info "Checksum verified"

  info "Extracting and installing binaries to $INSTALL_DIR..."
  tar -xzf "$TMPDIR/$TARBALL" -C "$TMPDIR"

  RELEASE_DIR="$TMPDIR/release-${FC_VERSION}-${ARCH}"
  install -m 755 "$RELEASE_DIR/firecracker-${FC_VERSION}-${ARCH}" "$INSTALL_DIR/firecracker"
  install -m 755 "$RELEASE_DIR/jailer-${FC_VERSION}-${ARCH}" "$INSTALL_DIR/jailer"

  info "firecracker installed: $(firecracker --version 2>/dev/null | head -1)"
  info "jailer installed: $(jailer --version 2>/dev/null | head -1)"
fi

# Kernel download: Flint's guest kernel (scripts/build-kernel.sh), which boots on
# Firecracker and Cloud Hypervisor and has the NFS client volumes need.
if [ "$SKIP_KERNEL" = "1" ]; then
  skip "Skipping vmlinux download (SKIP_KERNEL=1)"
elif [ "$ARCH" != "x86_64" ]; then
  skip "No Flint guest kernel for $ARCH yet: installing Firecracker's quickstart kernel (volumes won't work)"
  if [ ! -f "$KERNEL_DIR/vmlinux" ]; then
    mkdir -p "$KERNEL_DIR"
    curl -fsSL -o "$KERNEL_DIR/vmlinux" \
      "https://s3.amazonaws.com/spec.ccfc.min/img/quickstart_guide/${ARCH}/kernels/vmlinux.bin"
  fi
else
  VMLINUX_PATH="$KERNEL_DIR/vmlinux"
  KERNEL_BASE="https://github.com/jverre/flint/releases/download/${KERNEL_RELEASE}"
  info "Downloading Flint guest kernel ($KERNEL_RELEASE)..."
  curl -fsSL -o "$TMPDIR/vmlinux-$ARCH" "$KERNEL_BASE/vmlinux-$ARCH"
  curl -fsSL -o "$TMPDIR/vmlinux-$ARCH.sha256" "$KERNEL_BASE/vmlinux-$ARCH.sha256"
  (cd "$TMPDIR" && sha256sum -c "vmlinux-$ARCH.sha256" --status) || error "Kernel checksum verification failed"
  if [ -f "$VMLINUX_PATH" ] && cmp -s "$VMLINUX_PATH" "$TMPDIR/vmlinux-$ARCH"; then
    skip "vmlinux at $VMLINUX_PATH is already $KERNEL_RELEASE"
  else
    mkdir -p "$KERNEL_DIR"
    [ -f "$VMLINUX_PATH" ] && mv "$VMLINUX_PATH" "$VMLINUX_PATH.old" && info "Previous kernel kept at $VMLINUX_PATH.old"
    install -m 644 "$TMPDIR/vmlinux-$ARCH" "$VMLINUX_PATH"
    info "vmlinux installed at $VMLINUX_PATH ($KERNEL_RELEASE); the daemon rebuilds its golden snapshot on next start"
  fi
fi

printf "\n  Done. Installed:\n"
firecracker --version 2>/dev/null | head -1 | sed 's/^/    /'
jailer --version 2>/dev/null | head -1 | sed 's/^/    /' || true
[ -f "$KERNEL_DIR/vmlinux" ] && printf "    vmlinux at %s\n" "$KERNEL_DIR/vmlinux" || true
