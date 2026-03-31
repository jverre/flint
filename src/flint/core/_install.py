"""Install host runtime dependencies and guest assets."""

import hashlib
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

import sys

from .config import VZ_KERNEL_PATH, VZ_ROOTFS_PATH


# ── macOS Virtualization.framework entitlement signing ──────────────────────

_VZ_ENTITLEMENTS_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>com.apple.security.virtualization</key>
    <true/>
</dict>
</plist>"""


def _python_has_vz_entitlement(binary: str) -> bool:
    try:
        result = subprocess.run(
            ["codesign", "-d", "--entitlements", "-", binary],
            capture_output=True, text=True,
        )
        return "com.apple.security.virtualization" in result.stdout
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def ensure_vz_entitlement() -> None:
    """Ensure the running Python binary has the macOS virtualization entitlement.

    On macOS arm64, Apple's Virtualization.framework requires the process to
    carry ``com.apple.security.virtualization``.  If the current binary lacks
    it this function:

    1. Replaces the venv ``python`` symlink with a real copy of the binary.
    2. Ad-hoc signs it with the required entitlement.
    3. ``os.execv``\\s so the rest of the daemon runs inside the entitled process.

    No-op on non-macOS, non-arm64, or when already entitled.
    """
    if platform.system() != "Darwin" or platform.machine() not in ("arm64", "aarch64"):
        return

    real_exe = os.path.realpath(sys.executable)
    if _python_has_vz_entitlement(real_exe):
        return

    # Locate the venv's ``python`` — it's the file the other symlinks resolve through.
    venv_bin = os.path.dirname(os.path.abspath(sys.executable))
    venv_python = os.path.join(venv_bin, "python")

    if not os.path.exists(venv_python):
        _error(f"Cannot locate venv python at {venv_python} — skipping entitlement signing")
        return

    # If it's still a symlink, replace with a copy of the real binary.
    if os.path.islink(venv_python):
        real = os.path.realpath(venv_python)
        os.remove(venv_python)
        shutil.copy2(real, venv_python)

    # Write the entitlements plist once.
    plist_path = os.path.join(tempfile.gettempdir(), "flint-vz-entitlements.plist")
    with open(plist_path, "w") as f:
        f.write(_VZ_ENTITLEMENTS_PLIST)

    _info("Signing Python binary with macOS virtualization entitlement…")
    subprocess.run(
        ["codesign", "--entitlements", plist_path, "--force", "-s", "-", venv_python],
        check=True,
    )
    _info("Signed — re-executing under entitled binary")

    # Re-exec: the current process is replaced; PID stays the same.
    os.execv(venv_python, [venv_python] + sys.argv)


def _info(msg: str) -> None:
    print(f"  [+] {msg}")


def _skip(msg: str) -> None:
    print(f"  [!] {msg}")


def _error(msg: str) -> None:
    print(f"  [x] {msg}")


def _resolve_version(fc_version: str) -> str:
    """Resolve 'latest' to an actual tag via GitHub redirect."""
    if fc_version != "latest":
        return fc_version
    _info("Resolving latest Firecracker version...")
    req = urllib.request.Request(
        "https://github.com/firecracker-microvm/firecracker/releases/latest",
        method="HEAD",
    )
    with urllib.request.urlopen(req) as resp:
        resolved = resp.url.rstrip("/").split("/")[-1]
    _info(f"Latest version: {resolved}")
    return resolved


def _detect_arch() -> str:
    arch = platform.machine()
    if arch not in ("x86_64", "aarch64"):
        raise RuntimeError(f"Unsupported architecture: {arch}")
    return arch


def _download(url: str, dest: Path) -> None:
    with urllib.request.urlopen(url) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _require_command(cmd: str, *, install_hint: str | None = None) -> None:
    if shutil.which(cmd):
        return
    hint = f" Install {install_hint} and try again." if install_hint else ""
    raise RuntimeError(f"Required command not found: {cmd}.{hint}")


def _require_docker() -> None:
    _require_command("docker", install_hint="Docker Desktop")
    try:
        subprocess.run(
            ["docker", "info"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            "Docker is installed but not available. Start Docker Desktop and try again."
        ) from exc


def _require_macos_arm64() -> None:
    system = platform.system()
    arch = platform.machine().lower()
    if system != "Darwin" or arch not in ("arm64", "aarch64"):
        raise RuntimeError("This command only supports macOS on Apple Silicon.")


def _resolve_vz_paths(vz_dir: str | None = None) -> tuple[Path, Path]:
    if vz_dir:
        base = Path(vz_dir).expanduser()
        return base / "vmlinux", base / "rootfs.img"
    return Path(VZ_KERNEL_PATH).expanduser(), Path(VZ_ROOTFS_PATH).expanduser()


def _copy_atomic(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dest)


def _download_macos_kernel(dest: Path, kernel_release: str, kernel_build: str) -> None:
    url = (
        "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/"
        f"v{kernel_release}/aarch64/vmlinux-{kernel_build}"
    )
    _info(f"Downloading Linux arm64 kernel to {dest}...")
    dest.parent.mkdir(parents=True, exist_ok=True)
    _download(url, dest)
    size_mb = dest.stat().st_size // (1024 * 1024)
    _info(f"Kernel installed at {dest} ({size_mb}M)")


def _build_flintd_linux_arm64(output_path: Path) -> None:
    source_dir = _project_root() / "guest" / "flintd"
    if not source_dir.is_dir():
        raise FileNotFoundError(f"flintd source not found: {source_dir}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    _info("Building flintd guest agent for linux/arm64...")
    local_go = shutil.which("go")
    if local_go:
        env = os.environ.copy()
        env["CGO_ENABLED"] = "0"
        env["GOOS"] = "linux"
        env["GOARCH"] = "arm64"
        subprocess.run(
            [local_go, "build", "-buildvcs=false", "-ldflags=-s -w", "-o", str(output_path), "."],
            cwd=source_dir,
            check=True,
            env=env,
        )
    else:
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{source_dir}:/src",
                "-v",
                f"{output_path.parent}:/out",
                "-w",
                "/src",
                "golang:1.24-alpine",
                "sh",
                "-lc",
                "CGO_ENABLED=0 GOOS=linux GOARCH=arm64 /usr/local/go/bin/go build -buildvcs=false -ldflags='-s -w' -o /out/flintd .",
            ],
            check=True,
        )
    output_path.chmod(0o755)
    _info(f"Built flintd at {output_path}")


def _prepare_macos_rootfs_tree(rootfs_dir: Path, alpine_tarball: Path, flintd_path: Path) -> None:
    with tarfile.open(alpine_tarball, "r:gz") as tf:
        tf.extractall(rootfs_dir)

    init_script = _project_root() / "assets" / "init-vz.sh"
    if not init_script.exists():
        raise FileNotFoundError(f"Missing macOS init script: {init_script}")

    (rootfs_dir / "usr" / "local" / "bin").mkdir(parents=True, exist_ok=True)
    (rootfs_dir / "var" / "log").mkdir(parents=True, exist_ok=True)
    shutil.copy2(flintd_path, rootfs_dir / "usr" / "local" / "bin" / "flintd")
    shutil.copy2(init_script, rootfs_dir / "etc" / "init-net.sh")
    os.chmod(rootfs_dir / "usr" / "local" / "bin" / "flintd", 0o755)
    os.chmod(rootfs_dir / "etc" / "init-net.sh", 0o755)


def _build_ext4_image_with_docker(rootfs_dir: Path, dest: Path, size_mb: int) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    work_dir = dest.parent
    out_name = "rootfs.img"
    _info(f"Building ext4 rootfs image ({size_mb} MB) at {dest}...")
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{rootfs_dir}:/rootfs:ro",
            "-v",
            f"{work_dir}:/out",
            "alpine:3.21",
            "sh",
            "-lc",
            (
                "set -eu; "
                "apk add --no-cache e2fsprogs >/dev/null; "
                "mkdir -p /tmp/rootfs; "
                "cp -a /rootfs/. /tmp/rootfs/; "
                "chown -R 0:0 /tmp/rootfs; "
                f"rm -f /out/{out_name}; "
                f"truncate -s {size_mb}M /out/{out_name}; "
                f"mke2fs -q -d /tmp/rootfs -t ext4 -F /out/{out_name}"
            ),
        ],
        check=True,
    )
    _info(f"Rootfs image installed at {dest}")


def check_macos_vz_assets(vz_dir: str | None = None) -> bool:
    """Print macOS guest asset status. Returns True if both assets are present."""
    kernel_path, rootfs_path = _resolve_vz_paths(vz_dir)
    all_present = True

    if kernel_path.exists():
        size_mb = kernel_path.stat().st_size // (1024 * 1024)
        print(f"  vz kernel    {kernel_path} ({size_mb}M)")
    else:
        print(f"  vz kernel    NOT FOUND (expected at {kernel_path})")
        all_present = False

    if rootfs_path.exists():
        size_mb = rootfs_path.stat().st_size // (1024 * 1024)
        print(f"  vz rootfs    {rootfs_path} ({size_mb}M)")
    else:
        print(f"  vz rootfs    NOT FOUND (expected at {rootfs_path})")
        all_present = False

    return all_present


def setup_macos_vz(
    *,
    vz_dir: str | None = None,
    alpine_version: str = "3.21.3",
    kernel_version: str = "1.12",
    kernel_patch: str = "6.1.128",
    rootfs_size_mb: int = 1024,
    force: bool = False,
) -> None:
    """Prepare macOS Virtualization.framework guest assets for Flint."""
    _require_macos_arm64()
    _require_docker()

    kernel_path, rootfs_path = _resolve_vz_paths(vz_dir)
    asset_dir = kernel_path.parent
    if rootfs_path.parent != asset_dir:
        rootfs_path.parent.mkdir(parents=True, exist_ok=True)
    asset_dir.mkdir(parents=True, exist_ok=True)

    if check_macos_vz_assets(vz_dir=vz_dir) and not force:
        _skip("macOS guest assets already exist - skipping (use --force to rebuild)")
        print("\n  Done.")
        return

    with tempfile.TemporaryDirectory(prefix="flint-vz-setup-") as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        alpine_tarball = tmpdir / f"alpine-minirootfs-{alpine_version}-aarch64.tar.gz"
        flintd_path = tmpdir / "flintd"
        rootfs_dir = tmpdir / "rootfs"
        rootfs_image = tmpdir / "rootfs.img"

        if force:
            for path in (kernel_path, rootfs_path):
                if path.exists():
                    _skip(f"Removing existing asset {path}")
                    path.unlink()

        if not kernel_path.exists():
            _download_macos_kernel(kernel_path, kernel_version, kernel_patch)
        else:
            _skip(f"Kernel already exists at {kernel_path} - skipping")

        if not rootfs_path.exists():
            alpine_url = (
                "https://dl-cdn.alpinelinux.org/alpine/"
                f"v{'.'.join(alpine_version.split('.')[:2])}/releases/aarch64/"
                f"alpine-minirootfs-{alpine_version}-aarch64.tar.gz"
            )
            _info(f"Downloading Alpine minirootfs {alpine_version}...")
            _download(alpine_url, alpine_tarball)
            _build_flintd_linux_arm64(flintd_path)
            rootfs_dir.mkdir(parents=True, exist_ok=True)
            _prepare_macos_rootfs_tree(rootfs_dir, alpine_tarball, flintd_path)
            _build_ext4_image_with_docker(rootfs_dir, rootfs_image, rootfs_size_mb)
            _copy_atomic(rootfs_image, rootfs_path)
        else:
            _skip(f"Rootfs image already exists at {rootfs_path} - skipping")

    print("")
    print("  macOS guest assets are ready.")
    print(f"  kernel: {kernel_path}")
    print(f"  rootfs: {rootfs_path}")
    print("  Next: run 'uv run flint start'")


def _verify_checksum(tarball: Path, checksum_file: Path) -> None:
    """Verify SHA256 of tarball against the published checksum file."""
    sha256 = hashlib.sha256(tarball.read_bytes()).hexdigest()
    for line in checksum_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            expected_hash = parts[0]
            filename = parts[1].lstrip("*")
            if filename == tarball.name or Path(filename).name == tarball.name:
                if sha256 != expected_hash:
                    raise RuntimeError(
                        f"Checksum mismatch for {tarball.name}: "
                        f"expected {expected_hash}, got {sha256}"
                    )
                return
    # If the checksum file only has one entry, compare directly
    parts = checksum_file.read_text().strip().split()
    if parts and sha256 == parts[0]:
        return
    raise RuntimeError(f"Could not find checksum for {tarball.name} in checksum file")


def _extract_and_install(tarball: Path, tmpdir: Path, fc_version: str, arch: str, install_dir: str) -> None:
    with tarfile.open(tarball, "r:gz") as tf:
        tf.extractall(tmpdir)

    release_dir = tmpdir / f"release-{fc_version}-{arch}"
    fc_bin = release_dir / f"firecracker-{fc_version}-{arch}"
    jailer_bin = release_dir / f"jailer-{fc_version}-{arch}"

    install_path = Path(install_dir)
    for src, name in [(fc_bin, "firecracker"), (jailer_bin, "jailer")]:
        dst = install_path / name
        shutil.copy2(src, dst)
        dst.chmod(0o755)


def _install_kernel(kernel_version: str, kernel_patch: str, arch: str, kernel_dir: str) -> None:
    url = f"https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v{kernel_version}/{arch}/vmlinux-{kernel_patch}"
    dest = Path(kernel_dir) / "vmlinux"
    dest.parent.mkdir(parents=True, exist_ok=True)
    _info(f"Downloading vmlinux kernel ({kernel_version}) to {kernel_dir}...")
    _download(url, dest)
    size_mb = dest.stat().st_size // (1024 * 1024)
    _info(f"vmlinux installed at {dest} ({size_mb}M)")


def _current_fc_version() -> str | None:
    try:
        out = subprocess.check_output(["firecracker", "--version"], text=True)
        return out.strip().split()[-1]
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None


def check_deps(
    install_dir: str = "/usr/local/bin",
    kernel_dir: str = "/root/firecracker-vm",
) -> bool:
    """Print status of installed deps. Returns True if all present."""
    all_present = True

    fc_version = _current_fc_version()
    if fc_version:
        print(f"  firecracker  {fc_version}")
    else:
        print("  firecracker  NOT FOUND")
        all_present = False

    jailer_path = Path(install_dir) / "jailer"
    if jailer_path.exists():
        try:
            out = subprocess.check_output(["jailer", "--version"], text=True)
            print(f"  jailer       {out.strip().split()[-1]}")
        except (FileNotFoundError, subprocess.CalledProcessError):
            print("  jailer       found (version unknown)")
    else:
        print("  jailer       NOT FOUND")
        all_present = False

    vmlinux = Path(kernel_dir) / "vmlinux"
    if vmlinux.exists():
        size_mb = vmlinux.stat().st_size // (1024 * 1024)
        print(f"  vmlinux      {vmlinux} ({size_mb}M)")
    else:
        print(f"  vmlinux      NOT FOUND (expected at {vmlinux})")
        all_present = False

    return all_present


def install_deps(
    fc_version: str = "latest",
    install_dir: str = "/usr/local/bin",
    kernel_dir: str = "/root/firecracker-vm",
    kernel_version: str = "6.1",
    kernel_patch: str = "5.10.217",
    skip_kernel: bool = False,
) -> None:
    """Install firecracker, jailer, and optionally the vmlinux kernel."""
    if os.geteuid() != 0:
        _error("This command must be run as root (sudo flint install-deps)")
        raise SystemExit(1)

    arch = _detect_arch()
    fc_version = _resolve_version(fc_version)

    # Check if already at target version
    installed = _current_fc_version()
    if installed == fc_version:
        _skip(f"firecracker {fc_version} already installed - skipping download")
    else:
        tarball_name = f"firecracker-{fc_version}-{arch}.tgz"
        checksum_name = f"{tarball_name}.sha256.txt"
        release_base = f"https://github.com/firecracker-microvm/firecracker/releases/download/{fc_version}"

        _info(f"Downloading firecracker {fc_version} for {arch}...")
        with tempfile.TemporaryDirectory() as tmpdir_str:
            tmpdir = Path(tmpdir_str)
            tarball = tmpdir / tarball_name
            checksum_file = tmpdir / checksum_name

            _download(f"{release_base}/{tarball_name}", tarball)
            _download(f"{release_base}/{checksum_name}", checksum_file)

            _info("Verifying SHA256 checksum...")
            _verify_checksum(tarball, checksum_file)
            _info("Checksum verified")

            _info(f"Extracting and installing binaries to {install_dir}...")
            _extract_and_install(tarball, tmpdir, fc_version, arch, install_dir)

        fc_ver_out = _current_fc_version() or "unknown"
        _info(f"firecracker installed: Firecracker {fc_ver_out}")
        try:
            jailer_ver = subprocess.check_output(["jailer", "--version"], text=True).strip()
            _info(f"jailer installed: {jailer_ver}")
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass

    if skip_kernel:
        _skip("Skipping vmlinux download (--skip-kernel)")
    else:
        vmlinux = Path(kernel_dir) / "vmlinux"
        if vmlinux.exists():
            _skip(f"vmlinux already exists at {vmlinux} - skipping")
        else:
            _install_kernel(kernel_version, kernel_patch, arch, kernel_dir)

    print("\n  Done.")
