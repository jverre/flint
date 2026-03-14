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
    return [{"template_id": k, **v} for k, v in registry.items()]


def get_template(template_id: str) -> dict | None:
    registry = _load_registry()
    entry = registry.get(template_id)
    if entry is None:
        return None
    return {"template_id": template_id, **entry}


def register_template(
    template_id: str,
    name: str,
    template_dir: str,
    *,
    status: str = "ready",
    rootfs_size_mb: int | None = None,
) -> None:
    registry = _load_registry()
    registry[template_id] = {
        "name": name,
        "status": status,
        "created_at": time.time(),
        "template_dir": template_dir,
        "rootfs_size_mb": rootfs_size_mb,
    }
    _save_registry(registry)
    log.info("Registered template %s (%s)", template_id, name)


def update_template_status(template_id: str, status: str) -> None:
    registry = _load_registry()
    if template_id in registry:
        registry[template_id]["status"] = status
        _save_registry(registry)


def delete_template(template_id: str) -> None:
    if template_id == DEFAULT_TEMPLATE_ID:
        raise ValueError("Cannot delete the default template")
    registry = _load_registry()
    registry.pop(template_id, None)
    _save_registry(registry)
    log.info("Deleted template %s", template_id)


def get_template_dir(template_id: str) -> str:
    if template_id == DEFAULT_TEMPLATE_ID:
        return GOLDEN_DIR
    entry = get_template(template_id)
    if entry is None:
        raise KeyError(f"Template not found: {template_id}")
    return entry["template_dir"]


def template_snapshot_exists(template_id: str) -> bool:
    try:
        tdir = get_template_dir(template_id)
    except KeyError:
        return False
    return all(os.path.exists(f"{tdir}/{f}") for f in ("rootfs.ext4", "vmstate", "mem"))


def registered_template_ids() -> list[str]:
    return list(_load_registry().keys())
