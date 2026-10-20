#!/usr/bin/env bash
# Build the admin web UI (web/) and place it where Flint serves it from:
#   - src/flint/daemon/ui            → served by the daemon at /ui (and shipped in the wheel)
#   - cluster/internal/webui/dist    → embedded into flint-cluster at build time
# Needs Node 20+. Usage: scripts/build-ui.sh [--skip-install]
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root/web"
if [[ "${1:-}" != "--skip-install" ]]; then npm ci --no-audit --no-fund; fi
npx tsc -b
FLINT_UI_OUT="$root/src/flint/daemon/ui" npx vite build
dest="$root/cluster/internal/webui/dist"
find "$dest" -mindepth 1 ! -name .gitkeep -delete
cp -R "$root/src/flint/daemon/ui/." "$dest/"
echo "Web UI built: $root/src/flint/daemon/ui (daemon) and $dest (control plane)"
