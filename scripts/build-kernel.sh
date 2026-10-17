#!/usr/bin/env bash
# Build Flint's guest kernel: Firecracker's CI config for this kernel
# version, plus assets/kernel/flint.config. Boots on both backends.
#
#   scripts/build-kernel.sh [OUTPUT]     (default: ./vmlinux)
#
# Env: KERNEL_VERSION (default 6.1.141), FC_CI_VERSION (default v1.13),
#      BUILD_DIR (default: a temp dir), JOBS (default: nproc).
# Needs: gcc make flex bison bc libelf-dev libssl-dev.
#
# Releasing (install-deps downloads the release, not a local build):
#   cp vmlinux vmlinux-x86_64 && sha256sum vmlinux-x86_64 > vmlinux-x86_64.sha256
#   cp "$BUILD_DIR/linux-$KERNEL_VERSION/.config" config-x86_64
#   gh release create guest-kernel-<version>-flintN vmlinux-x86_64 vmlinux-x86_64.sha256 config-x86_64
# then bump KERNEL_RELEASE in scripts/install-deps.sh and src/flint/core/_install.py.
set -euo pipefail

KERNEL_VERSION="${KERNEL_VERSION:-6.1.141}"
FC_CI_VERSION="${FC_CI_VERSION:-v1.13}"
OUTPUT="$(realpath -m "${1:-vmlinux}")"
FRAGMENT="$(cd "$(dirname "$0")/.." && pwd)/assets/kernel/flint.config"
BUILD_DIR="${BUILD_DIR:-$(mktemp -d)}"
ARCH_DIR="$(uname -m)"

mkdir -p "$BUILD_DIR" && cd "$BUILD_DIR"
if [ ! -d "linux-$KERNEL_VERSION" ]; then
  curl -fsSL "https://cdn.kernel.org/pub/linux/kernel/v${KERNEL_VERSION%%.*}.x/linux-$KERNEL_VERSION.tar.xz" | tar xJ
fi
cd "linux-$KERNEL_VERSION"
curl -fsSL -o .config \
  "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/$FC_CI_VERSION/$ARCH_DIR/vmlinux-$KERNEL_VERSION.config"
scripts/kconfig/merge_config.sh -m .config "$FRAGMENT" >/dev/null
make olddefconfig >/dev/null

# merge_config doesn't fail on options Kconfig dropped; check ourselves.
missing=0
while IFS= read -r line; do
  case "$line" in CONFIG_*=*) grep -qx "$line" .config || { echo "not set: $line" >&2; missing=1; } ;; esac
done < "$FRAGMENT"
[ "$missing" = 0 ]

make -j"${JOBS:-$(nproc)}" vmlinux
install -m 644 vmlinux "$OUTPUT"
echo "Built $OUTPUT ($KERNEL_VERSION, Firecracker CI $FC_CI_VERSION config + flint.config)"
