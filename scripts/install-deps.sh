#!/usr/bin/env bash
set -euo pipefail

# Firecracker + jailer + vmlinux installer
# Usage: curl -fsSL https://raw.githubusercontent.com/jacquesverre/flint/main/scripts/install-deps.sh | sudo sh
# Env vars:
#   FC_VERSION    - firecracker version to install (default: latest)
#   INSTALL_DIR   - where to install binaries (default: /usr/local/bin)
#   KERNEL_DIR    - where to store vmlinux (default: /root/firecracker-vm)
#   SKIP_KERNEL   - set to 1 to skip vmlinux download (default: 0)

FC_VERSION="${FC_VERSION:-latest}"
INSTALL_DIR="${INSTALL_DIR:-/usr/local/bin}"
KERNEL_DIR="${KERNEL_DIR:-/root/firecracker-vm}"
SKIP_KERNEL="${SKIP_KERNEL:-0}"

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

# Kernel download
if [ "$SKIP_KERNEL" = "1" ]; then
  skip "Skipping vmlinux download (SKIP_KERNEL=1)"
else
  VMLINUX_PATH="$KERNEL_DIR/vmlinux"
  KERNEL_URL="https://s3.amazonaws.com/spec.ccfc.min/img/quickstart_guide/${ARCH}/kernels/vmlinux.bin"

  if [ -f "$VMLINUX_PATH" ]; then
    skip "vmlinux already exists at $VMLINUX_PATH - skipping"
  else
    info "Downloading vmlinux kernel to $KERNEL_DIR..."
    mkdir -p "$KERNEL_DIR"
    curl -fsSL -o "$VMLINUX_PATH" "$KERNEL_URL"
    info "vmlinux installed at $VMLINUX_PATH ($(du -sh "$VMLINUX_PATH" | cut -f1))"
  fi
fi

printf "\n  Done. Installed:\n"
firecracker --version 2>/dev/null | head -1 | sed 's/^/    /'
jailer --version 2>/dev/null | head -1 | sed 's/^/    /' || true
[ -f "$KERNEL_DIR/vmlinux" ] && printf "    vmlinux at %s\n" "$KERNEL_DIR/vmlinux" || true
