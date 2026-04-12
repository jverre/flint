"""Local OCI image cache for Flint.

Tracks pulled images by digest so unchanged images are not
re-downloaded. Cached rootfs.ext4 files can be copied directly
with ``cp --reflink=auto`` for near-zero-cost cloning.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

from .config import log, OCI_CACHE_DIR


def _meta_path() -> str:
    return os.path.join(OCI_CACHE_DIR, "cache.json")


def _load_meta() -> dict:
    path = _meta_path()
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)


def _save_meta(data: dict) -> None:
    os.makedirs(OCI_CACHE_DIR, exist_ok=True)
    path = _meta_path()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.rename(tmp, path)


def get(image_ref: str, current_digest: str) -> str | None:
    """Return cached rootfs path if the image digest matches, else None."""
    meta = _load_meta()
    entry = meta.get(image_ref)
    if not entry:
        return None
    if entry.get("digest") != current_digest:
        log.debug("oci-cache: stale entry for %s (cached=%s, current=%s)",
                  image_ref, entry.get("digest", "")[:16], current_digest[:16])
        return None
    rootfs_path = entry.get("rootfs_path", "")
    if not os.path.exists(rootfs_path):
        log.debug("oci-cache: rootfs missing for %s", image_ref)
        return None
    # Update access time for LRU
    entry["last_access"] = time.time()
    meta[image_ref] = entry
    _save_meta(meta)
    log.info("oci-cache: hit for %s", image_ref)
    return rootfs_path


def put(image_ref: str, digest: str, rootfs_path: str) -> None:
    """Store a pulled image in the cache."""
    os.makedirs(OCI_CACHE_DIR, exist_ok=True)

    # Copy rootfs into cache dir
    cache_id = digest.replace("sha256:", "")[:16]
    cached_rootfs = os.path.join(OCI_CACHE_DIR, f"{cache_id}-rootfs.ext4")
    subprocess.run(
        ["cp", "--reflink=auto", rootfs_path, cached_rootfs],
        check=True,
    )

    meta = _load_meta()
    meta[image_ref] = {
        "digest": digest,
        "rootfs_path": cached_rootfs,
        "pulled_at": time.time(),
        "last_access": time.time(),
    }
    _save_meta(meta)
    log.info("oci-cache: stored %s (digest=%s)", image_ref, digest[:20])


def evict(image_ref: str) -> None:
    """Remove a cached image."""
    meta = _load_meta()
    entry = meta.pop(image_ref, None)
    if entry:
        rootfs = entry.get("rootfs_path", "")
        if rootfs and os.path.exists(rootfs):
            os.unlink(rootfs)
        _save_meta(meta)
        log.info("oci-cache: evicted %s", image_ref)


def cleanup(max_entries: int = 20) -> None:
    """Evict least-recently-used entries until cache has at most *max_entries*."""
    meta = _load_meta()
    if len(meta) <= max_entries:
        return

    # Sort by last_access ascending (oldest first)
    sorted_refs = sorted(meta.keys(), key=lambda r: meta[r].get("last_access", 0))
    to_evict = len(meta) - max_entries
    for ref in sorted_refs[:to_evict]:
        entry = meta.pop(ref)
        rootfs = entry.get("rootfs_path", "")
        if rootfs and os.path.exists(rootfs):
            os.unlink(rootfs)
        log.debug("oci-cache: gc evicted %s", ref)

    _save_meta(meta)
    log.info("oci-cache: gc removed %d entries", to_evict)


def copy_cached_rootfs(cached_path: str, dest_path: str) -> None:
    """Copy a cached rootfs to a destination using reflink if available."""
    subprocess.run(
        ["cp", "--reflink=auto", cached_path, dest_path],
        check=True,
    )
