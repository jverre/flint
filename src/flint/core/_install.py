"""Install firecracker, jailer, and vmlinux kernel."""

import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path


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
    import platform
    arch = platform.machine()
    if arch not in ("x86_64", "aarch64"):
        raise RuntimeError(f"Unsupported architecture: {arch}")
    return arch


def _download(url: str, dest: Path) -> None:
    with urllib.request.urlopen(url) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


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
