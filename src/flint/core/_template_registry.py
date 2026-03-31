"""File-based template registry backed by registry.json."""

from __future__ import annotations

import json
import os
import time

from .config import log, TEMPLATES_DIR, DEFAULT_TEMPLATE_ID, GOLDEN_DIR


def _registry_path() -> str:
    return f"{TEMPLATES_DIR}/registry.json"


def _load_registry() -> dict:
    path = _registry_path()
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)


def _save_registry(data: dict) -> None:
    os.makedirs(TEMPLATES_DIR, exist_ok=True)
    path = _registry_path()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.rename(tmp, path)


def list_templates() -> list[dict]:
    registry = _load_registry()
    return [{"template_id": k, **_normalize_entry(v)} for k, v in registry.items()]


def get_template(template_id: str) -> dict | None:
    registry = _load_registry()
    entry = registry.get(template_id)
    if entry is None:
        return None
    return {"template_id": template_id, **_normalize_entry(entry)}


def _normalize_entry(entry: dict) -> dict:
    entry = dict(entry)
    if "artifacts" not in entry:
        template_dir = entry.get("template_dir")
        status = entry.get("status", "ready")
        rootfs_size_mb = entry.get("rootfs_size_mb")
        artifacts = {}
        if template_dir:
            artifacts["linux-firecracker"] = {
                "template_dir": template_dir,
                "status": status,
                "rootfs_size_mb": rootfs_size_mb,
            }
        entry["artifacts"] = artifacts
    return entry


def register_template(
    template_id: str,
    name: str,
    template_dir: str,
    *,
    status: str = "ready",
    rootfs_size_mb: int | None = None,
) -> None:
    registry = _load_registry()
    registry[template_id] = _normalize_entry({
        "name": name,
        "status": status,
        "created_at": time.time(),
        "template_dir": template_dir,
        "rootfs_size_mb": rootfs_size_mb,
    })
    _save_registry(registry)
    log.info("Registered template %s (%s)", template_id, name)


def register_template_artifact(
    template_id: str,
    name: str,
    backend_kind: str,
    template_dir: str,
    *,
    status: str = "ready",
    rootfs_size_mb: int | None = None,
) -> None:
    registry = _load_registry()
    existing = _normalize_entry(registry.get(template_id, {"name": name, "created_at": time.time()}))
    existing["name"] = name
    existing["status"] = status
    existing["template_dir"] = template_dir if backend_kind == "linux-firecracker" else existing.get("template_dir", template_dir)
    existing["rootfs_size_mb"] = rootfs_size_mb if rootfs_size_mb is not None else existing.get("rootfs_size_mb")
    artifacts = dict(existing.get("artifacts") or {})
    artifacts[backend_kind] = {
        "template_dir": template_dir,
        "status": status,
        "rootfs_size_mb": rootfs_size_mb,
    }
    existing["artifacts"] = artifacts
    registry[template_id] = existing
    _save_registry(registry)
    log.info("Registered template artifact %s (%s) for %s", template_id, name, backend_kind)


def update_template_status(template_id: str, status: str) -> None:
    registry = _load_registry()
    if template_id in registry:
        entry = _normalize_entry(registry[template_id])
        entry["status"] = status
        artifacts = entry.get("artifacts") or {}
        if len(artifacts) == 1:
            backend_kind = next(iter(artifacts))
            artifacts[backend_kind]["status"] = status
        entry["artifacts"] = artifacts
        registry[template_id] = entry
        _save_registry(registry)


def update_template_artifact_status(template_id: str, backend_kind: str, status: str) -> None:
    registry = _load_registry()
    if template_id not in registry:
        return
    entry = _normalize_entry(registry[template_id])
    artifacts = dict(entry.get("artifacts") or {})
    artifact = dict(artifacts.get(backend_kind) or {})
    if not artifact:
        return
    artifact["status"] = status
    artifacts[backend_kind] = artifact
    entry["artifacts"] = artifacts
    if backend_kind == "linux-firecracker":
        entry["status"] = status
    registry[template_id] = entry
    _save_registry(registry)


def delete_template(template_id: str) -> None:
    if template_id == DEFAULT_TEMPLATE_ID:
        raise ValueError("Cannot delete the default template")
    registry = _load_registry()
    registry.pop(template_id, None)
    _save_registry(registry)
    log.info("Deleted template %s", template_id)


def delete_template_artifact(template_id: str, backend_kind: str) -> None:
    registry = _load_registry()
    entry = registry.get(template_id)
    if entry is None:
        return
    entry = _normalize_entry(entry)
    artifacts = dict(entry.get("artifacts") or {})
    artifacts.pop(backend_kind, None)
    entry["artifacts"] = artifacts
    if not artifacts:
        registry.pop(template_id, None)
    else:
        if backend_kind == "linux-firecracker":
            fallback_dir = next(iter(artifacts.values())).get("template_dir")
            entry["template_dir"] = fallback_dir
        registry[template_id] = entry
    _save_registry(registry)


def get_template_dir(template_id: str, backend_kind: str = "linux-firecracker") -> str:
    if template_id == DEFAULT_TEMPLATE_ID:
        return GOLDEN_DIR
    entry = get_template(template_id)
    if entry is None:
        raise KeyError(f"Template not found: {template_id}")
    artifacts = entry.get("artifacts") or {}
    artifact = artifacts.get(backend_kind)
    if artifact:
        return artifact["template_dir"]
    return entry["template_dir"]


def template_snapshot_exists(template_id: str, backend_kind: str = "linux-firecracker") -> bool:
    try:
        tdir = get_template_dir(template_id, backend_kind=backend_kind)
    except KeyError:
        return False
    return all(os.path.exists(f"{tdir}/{f}") for f in ("rootfs.ext4", "vmstate", "mem"))


def registered_template_ids() -> list[str]:
    return list(_load_registry().keys())
