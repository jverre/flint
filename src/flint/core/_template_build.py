"""Template import: a container image becomes a bootable root disk and a golden snapshot.

The image is pulled and converted inside a disposable builder VM
(``_builder.import_rootfs``); the host only receives a size-bounded ext4
image, which it boots in a jail to take the golden snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile

from .config import log
from ._snapshot import create_golden_snapshot
from ._template_registry import update_template_artifact_status, _slugify

# Written beside the snapshot: which image, at which digest, the template holds.
SOURCE_FILE = "source.json"

# Docker-style references: [registry[:port]/]repo[:tag][@sha256:digest]. Strict,
# so a reference is never more than a name to the registry client.
_IMAGE_RE = re.compile(r"[a-z0-9][a-z0-9._\-/:]*(@sha256:[a-f0-9]{64})?", re.IGNORECASE)


def validate_image(image) -> str:
    if not isinstance(image, str) or len(image) > 512 or not _IMAGE_RE.fullmatch(image):
        raise ValueError("image must be a container image reference, e.g. python:3.12-slim")
    return image


def validate_auth(auth) -> dict | None:
    if auth is None:
        return None
    if (not isinstance(auth, dict) or set(auth) != {"username", "password"}
            or not all(isinstance(v, str) and v and len(v) <= 4096 for v in auth.values())):
        raise ValueError("registry_auth must be {\"username\": ..., \"password\": ...}")
    return auth


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


def _build_go(name: str, output_path: str) -> None:
    """Copy ``assets/<name>``, or build ``guest/<name>`` as a static linux/amd64 binary."""
    project_root = _find_project_root()
    prebuilt = os.path.join(project_root, "assets", name)
    if os.path.exists(prebuilt):
        shutil.copy2(prebuilt, output_path)
        log.info("Using pre-built %s from assets/ (%s)", name, prebuilt)
        return

    source_dir = os.path.join(project_root, "guest", name)
    if not os.path.isdir(source_dir):
        raise FileNotFoundError(
            f"{name} not found: checked {prebuilt} and {source_dir}. "
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
    log.info("Built %s from %s", name, source_dir)


def _build_flintd(output_path: str) -> None:
    _build_go("flintd", output_path)


def _asset(name: str) -> str:
    path = os.path.join(_find_project_root(), "assets", name)
    if not os.path.exists(path):
        raise FileNotFoundError(f"{name} not found at {path}")
    return path


def _find_init_net_sh() -> str:
    return _asset("init-net.sh")


def run_import(kind: str, name: str, image: str, rootfs_size_mb: int, auth: dict | None) -> dict:
    """A backend's ``import_template``: supervise a worker, or be the worker."""
    if not os.environ.get("FLINT_BUILD_JOB"):
        from ._build_job import run_build
        return run_build(kind, name, image, rootfs_size_mb, auth)
    return import_template(kind, name, image, rootfs_size_mb, auth)


def import_template(kind: str, name: str, image: str, rootfs_size_mb: int, auth: dict | None) -> dict:
    """Worker body: image -> rootfs (in a builder VM) -> golden snapshot, staged for publishing."""
    template_id = _slugify(name)
    from ._build_job import output_dir
    template_dir = output_dir(template_id)
    os.makedirs(template_dir, exist_ok=True)
    # The supervisor publishes the staged directory; the current template stays bootable meanwhile.
    update_template_artifact_status(template_id, kind, "building")

    context_dir = tempfile.mkdtemp(prefix="context-", dir=template_dir)
    try:
        log.info("[%s] Import of %s started (rootfs_size_mb=%d)", template_id, image, rootfs_size_mb)
        _build_flintd(os.path.join(context_dir, "flintd"))
        _build_go("flint-import", os.path.join(context_dir, "flint-import"))
        for asset in ("init-net.sh", "template-inject.sh"):
            shutil.copy2(_asset(asset), os.path.join(context_dir, asset))

        from ._builder import import_rootfs
        rootfs_path = f"{template_dir}/rootfs.ext4"
        source = import_rootfs(kind, image, auth, context_dir, rootfs_path, rootfs_size_mb)
        with open(os.path.join(template_dir, SOURCE_FILE), "w") as f:
            json.dump(source, f)

        if kind == "linux-firecracker":
            # TAP device names must be ≤15 chars (Linux IFNAMSIZ limit).
            h = hashlib.sha1(template_id.encode()).hexdigest()[:8]
            create_golden_snapshot(source_rootfs=rootfs_path, snapshot_dir=template_dir,
                                   ns_name=f"fc-tmpl-{h}", tap_name=f"tapt-{h}")
        else:
            from ._ch_boot import ch_create_golden_snapshot
            ch_create_golden_snapshot(template_dir)
        log.info("Template %s imported from %s (%s)", template_id, source["image"], source["digest"])
        return {"template_id": template_id, "status": "building"}
    except Exception:
        update_template_artifact_status(template_id, kind, "failed")
        raise
    finally:
        from ._wipe import wipe_tree
        wipe_tree(context_dir)


def read_source(template_dir: str) -> dict | None:
    try:
        with open(os.path.join(template_dir, SOURCE_FILE)) as f:
            data = json.load(f)
        return {"image": str(data["image"]), "digest": str(data["digest"])}
    except (OSError, ValueError, KeyError, TypeError):
        return None
