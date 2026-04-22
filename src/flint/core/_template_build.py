"""Template build pipeline: Dockerfile generation, Docker build, rootfs extraction."""

from __future__ import annotations

import os
import re
import shutil
import subprocess

from .config import log, TEMPLATES_DIR, KERNEL_PATH, BOOT_ARGS, GUEST_MAC
from ._snapshot import create_golden_snapshot
from ._template_registry import register_template_artifact, update_template_artifact_status


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "template"


def _generate_dockerfile(base_image: str, steps: list[dict], flint_injection: bool = True) -> str:
    lines = [f"FROM {base_image}", ""]
    for step in steps:
        kind = step["type"]
        if kind == "run":
            lines.append(f"RUN {step['cmd']}")
        elif kind == "apt_install":
            pkgs = " ".join(step["packages"])
            lines.append(f"RUN apt-get update && apt-get install -y {pkgs} && rm -rf /var/lib/apt/lists/*")
        elif kind == "pip_install":
            pkgs = " ".join(step["packages"])
            lines.append(f"RUN pip install --no-cache-dir {pkgs}")
        elif kind == "npm_install":
            pkgs = " ".join(step["packages"])
            lines.append(f"RUN npm install -g {pkgs}")
        elif kind == "copy":
            lines.append(f"COPY {step['src']} {step['dest']}")
        elif kind == "workdir":
            lines.append(f"WORKDIR {step['path']}")
        elif kind == "env":
            for k, v in step["envs"].items():
                lines.append(f"ENV {k}={v}")
        elif kind == "git_clone":
            lines.append(f"RUN git clone {step['repo']} {step.get('dest', '')}")

    if flint_injection:
        lines.append("")
        lines.append("# Flint injection (always last)")
        lines.append("RUN apt-get update && apt-get install -y iproute2 || apk add iproute2 || true")
        lines.append("COPY flintd /usr/local/bin/flintd")
        lines.append("COPY init-net.sh /etc/init-net.sh")
        lines.append("RUN chmod +x /usr/local/bin/flintd /etc/init-net.sh")

    return "\n".join(lines) + "\n"


def _find_project_root() -> str:
    """Locate the project root by checking __file__-based path and CWD fallback."""
    # Primary: walk up from this source file (works with editable installs)
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    if os.path.isdir(os.path.join(root, "assets")):
        return root
    # Fallback: check CWD (works when daemon runs from the repo checkout)
    cwd = os.getcwd()
    if os.path.isdir(os.path.join(cwd, "assets")):
        return cwd
    # Last resort: return the __file__-based root anyway
    log.warning("Project root may be incorrect: %s (assets/ not found)", root)
    return root


def _build_flintd(output_path: str) -> None:
    """Build flintd Go binary, or fall back to assets/ pre-built binary."""
    project_root = _find_project_root()
    prebuilt = os.path.join(project_root, "assets", "flintd")
    if os.path.exists(prebuilt):
        shutil.copy2(prebuilt, output_path)
        log.info("Using pre-built flintd from assets/ (%s)", prebuilt)
        return

    source_dir = os.path.join(project_root, "guest", "flintd")
    if not os.path.isdir(source_dir):
        raise FileNotFoundError(
            f"flintd not found: checked {prebuilt} and {source_dir}. "
            f"Project root resolved to: {project_root}"
        )

    env = os.environ.copy()
    env["CGO_ENABLED"] = "0"
    env["GOOS"] = "linux"
    env["GOARCH"] = "amd64"
    subprocess.run(
        ["go", "build", "-ldflags=-s -w", "-o", output_path, "."],
        cwd=source_dir,
        check=True,
        capture_output=True,
        env=env,
    )
    log.info("Built flintd from %s", source_dir)


def _find_init_net_sh() -> str:
    """Locate init-net.sh — check assets/ then the source rootfs."""
    project_root = _find_project_root()
    candidate = os.path.join(project_root, "assets", "init-net.sh")
    if os.path.exists(candidate):
        return candidate
    raise FileNotFoundError(
        f"init-net.sh not found at {candidate}. "
        f"Project root resolved to: {project_root}"
    )


def _docker_build(template_id: str, dockerfile_content: str, context_dir: str) -> str:
    tag = f"flint-template:{template_id}"
    dockerfile_path = os.path.join(context_dir, "Dockerfile")
    with open(dockerfile_path, "w") as f:
        f.write(dockerfile_content)

    log.info("Building Docker image %s ...", tag)
    log.info("Dockerfile:\n%s", dockerfile_content)
    log.info("Context dir contents: %s", os.listdir(context_dir))
    result = subprocess.run(
        ["docker", "build", "-t", tag, "-f", dockerfile_path, context_dir],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.error("Docker build FAILED (exit %d):\nstdout: %s\nstderr: %s",
                  result.returncode, result.stdout[-2000:], result.stderr[-2000:])
        raise RuntimeError(
            f"Docker build failed for {tag} (exit {result.returncode}): {result.stderr[-500:]}"
        )
    log.info("Docker build succeeded for %s", tag)
    return tag


def _extract_rootfs(image_tag: str, rootfs_path: str, size_mb: int) -> None:
    log.info("Extracting rootfs from %s (%d MB) ...", image_tag, size_mb)

    # Create empty ext4 image
    subprocess.run(["truncate", "-s", f"{size_mb}M", rootfs_path], check=True)
    subprocess.run(["mkfs.ext4", "-F", "-m", "0", rootfs_path], check=True, capture_output=True)

    # Mount, export docker filesystem, unmount
    mount_dir = f"/tmp/flint-rootfs-mount-{os.getpid()}"
    os.makedirs(mount_dir, exist_ok=True)
    try:
        subprocess.run(["mount", rootfs_path, mount_dir], check=True)

        # Create container, export, extract
        result = subprocess.run(
            ["docker", "create", image_tag],
            check=True, capture_output=True, text=True,
        )
        container_id = result.stdout.strip()
        try:
            export_proc = subprocess.Popen(
                ["docker", "export", container_id],
                stdout=subprocess.PIPE,
            )
            subprocess.run(
                ["tar", "-x", "-C", mount_dir],
                stdin=export_proc.stdout,
                check=True,
            )
            rc = export_proc.wait()
            if rc != 0:
                raise RuntimeError(f"docker export failed with exit code {rc}")
        finally:
            subprocess.run(["docker", "rm", container_id], capture_output=True)
    finally:
        subprocess.run(["umount", mount_dir], capture_output=True)
        shutil.rmtree(mount_dir, ignore_errors=True)

    log.info("Rootfs extracted to %s", rootfs_path)


def build_template(
    name: str,
    dockerfile_content: str,
    *,
    rootfs_size_mb: int = 500,
) -> str:
    """Full build pipeline: Docker build -> rootfs extraction -> golden snapshot -> register.

    Returns template_id.
    """
    template_id = _slugify(name)
    template_dir = f"{TEMPLATES_DIR}/{template_id}"
    os.makedirs(template_dir, exist_ok=True)

    # Register as "building"
    register_template_artifact(
        template_id,
        name,
        "linux-firecracker",
        template_dir,
        status="building",
        rootfs_size_mb=rootfs_size_mb,
    )

    try:
        # Prepare build context directory
        context_dir = f"/tmp/flint-build-{template_id}"
        os.makedirs(context_dir, exist_ok=True)
        log.info("[%s] Build started (rootfs_size_mb=%d)", template_id, rootfs_size_mb)

        # Copy flintd binary and init-net.sh into context
        log.info("[%s] Copying flintd and init-net.sh to build context...", template_id)
        _build_flintd(os.path.join(context_dir, "flintd"))
        init_net = _find_init_net_sh()
        shutil.copy2(init_net, os.path.join(context_dir, "init-net.sh"))

        # Save Dockerfile for reproducibility
        with open(f"{template_dir}/Dockerfile", "w") as f:
            f.write(dockerfile_content)

        # Docker build
        image_tag = _docker_build(template_id, dockerfile_content, context_dir)

        # Extract rootfs
        rootfs_path = f"{template_dir}/rootfs.ext4"
        _extract_rootfs(image_tag, rootfs_path, rootfs_size_mb)

        # Create golden snapshot for this template
        # TAP device names must be ≤15 chars (Linux IFNAMSIZ limit).
        # Use a short hash of the template_id to guarantee this.
        import hashlib
        _h = hashlib.sha1(template_id.encode()).hexdigest()[:8]
        ns_name = f"fc-tmpl-{_h}"
        tap_name = f"tapt-{_h}"
        create_golden_snapshot(
            source_rootfs=rootfs_path,
            snapshot_dir=template_dir,
            ns_name=ns_name,
            tap_name=tap_name,
        )

        # Update status
        update_template_artifact_status(template_id, "linux-firecracker", "ready")

        # Cleanup
        subprocess.run(["docker", "rmi", image_tag], capture_output=True)
        shutil.rmtree(context_dir, ignore_errors=True)

        log.info("Template %s built successfully", template_id)
        return template_id

    except Exception:
        update_template_artifact_status(template_id, "linux-firecracker", "failed")
        raise
