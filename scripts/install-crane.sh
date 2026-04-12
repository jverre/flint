#!/bin/bash
set -euo pipefail

# Install crane (go-containerregistry CLI) for OCI image operations.
# Usage: curl -fsSL <url> | sudo bash
#   or:  sudo bash scripts/install-crane.sh
#
# Environment variables:
#   CRANE_VERSION  - version to install (default: latest)
#   INSTALL_DIR    - installation directory (default: /usr/local/bin)

INSTALL_DIR="${INSTALL_DIR:-/usr/local/bin}"
CRANE_VERSION="${CRANE_VERSION:-}"

ARCH="$(uname -m)"
OS="$(uname -s)"

case "$ARCH" in
    x86_64)  ARCH="x86_64" ;;
    aarch64|arm64) ARCH="arm64" ;;
    *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

case "$OS" in
    Linux)  OS="Linux" ;;
    Darwin) OS="Darwin" ;;
    *) echo "Unsupported OS: $OS" >&2; exit 1 ;;
esac

# Resolve latest version if not specified
if [ -z "$CRANE_VERSION" ]; then
    CRANE_VERSION="$(curl -fsSL https://api.github.com/repos/google/go-containerregistry/releases/latest | grep '"tag_name"' | sed 's/.*"tag_name": "\(.*\)".*/\1/')"
    echo "Latest crane version: $CRANE_VERSION"
fi

TARBALL="go-containerregistry_${OS}_${ARCH}.tar.gz"
URL="https://github.com/google/go-containerregistry/releases/download/${CRANE_VERSION}/${TARBALL}"

echo "Downloading crane ${CRANE_VERSION} for ${OS}/${ARCH}..."
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

curl -fsSL -o "${TMPDIR}/${TARBALL}" "$URL"
tar -xzf "${TMPDIR}/${TARBALL}" -C "$TMPDIR" crane

install -m 0755 "${TMPDIR}/crane" "${INSTALL_DIR}/crane"
echo "crane installed to ${INSTALL_DIR}/crane"
"${INSTALL_DIR}/crane" version
