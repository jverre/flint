import os
import shutil
import subprocess
import threading
import uuid

from .config import log, POOL_DIR, POOL_TARGET_SIZE, POOL_WORKERS, SOURCE_ROOTFS, GOLDEN_DIR, DEFAULT_TEMPLATE_ID
from ._snapshot import golden_snapshot_exists

_pool_lock = threading.Lock()
_pool_entries: list[dict] = []
_pool_stop_event = threading.Event()
_pool_threads: list[threading.Thread] = []


TEMPLATE_POOL_SIZE = 2  # pool size for non-default templates


def _copy_one_to_pool(source_path: str, template_id: str) -> str | None:
    pool_id = str(uuid.uuid4())
    pool_entry_dir = f"{POOL_DIR}/{pool_id}"
    rootfs_dest = f"{pool_entry_dir}/rootfs.ext4"

    entry = {"id": pool_id, "dir_path": pool_entry_dir, "template_id": template_id, "state": "copying"}
    with _pool_lock:
        _pool_entries.append(entry)

    try:
        os.makedirs(pool_entry_dir, exist_ok=True)
        subprocess.run(["cp", "--reflink=auto", source_path, rootfs_dest], check=True)
        with _pool_lock:
            entry["state"] = "ready"
        return pool_id
    except Exception:
        log.exception("pool: failed to create entry %s", pool_id[:8])
        shutil.rmtree(pool_entry_dir, ignore_errors=True)
        with _pool_lock:
            _pool_entries.remove(entry)
        return None


def _pool_refill_loop():
    while not _pool_stop_event.is_set():
        try:
            filled = False
            # Refill default template
            with _pool_lock:
                default_count = sum(1 for e in _pool_entries if e["template_id"] == DEFAULT_TEMPLATE_ID and e["state"] in ("ready", "copying"))
            if default_count < POOL_TARGET_SIZE:
                if golden_snapshot_exists():
                    _copy_one_to_pool(f"{GOLDEN_DIR}/rootfs.ext4", DEFAULT_TEMPLATE_ID)
                else:
                    _copy_one_to_pool(SOURCE_ROOTFS, DEFAULT_TEMPLATE_ID)
                filled = True

            # Refill non-default templates
            from ._template_registry import registered_template_ids, template_snapshot_exists, get_template_dir
            for tid in registered_template_ids():
                if tid == DEFAULT_TEMPLATE_ID:
                    continue
                if not template_snapshot_exists(tid):
                    continue
                with _pool_lock:
                    tid_count = sum(1 for e in _pool_entries if e["template_id"] == tid and e["state"] in ("ready", "copying"))
                if tid_count < TEMPLATE_POOL_SIZE:
                    tdir = get_template_dir(tid)
                    _copy_one_to_pool(f"{tdir}/rootfs.ext4", tid)
                    filled = True

            if not filled:
                _pool_stop_event.wait(0.02)
        except Exception:
            log.exception("pool: refill loop error")
            _pool_stop_event.wait(1.0)


def _claim_pool_entry(template_id: str, vm_id: str) -> str | None:
    with _pool_lock:
        match = next((e for e in _pool_entries if e["state"] == "ready" and e["template_id"] == template_id), None)
        if not match:
            ready_count = sum(1 for e in _pool_entries if e["state"] == "ready")
            copying_count = sum(1 for e in _pool_entries if e["state"] == "copying")
            log.debug("pool: miss for %s (ready=%d, copying=%d)", template_id, ready_count, copying_count)
            return None
        _pool_entries.remove(match)
        remaining = sum(1 for e in _pool_entries if e["state"] == "ready" and e["template_id"] == template_id)
    log.debug("pool: claimed %s for %s (%d remaining)", match["id"][:8], vm_id[:8], remaining)

    vm_dir = f"/microvms/{vm_id}"
    try:
        os.rename(match["dir_path"], vm_dir)
        return vm_dir
    except OSError:
        log.exception("pool: rename %s -> %s failed", match["dir_path"], vm_dir)
        shutil.rmtree(match["dir_path"], ignore_errors=True)
        return None


def start_pool():
    global _pool_threads
    _pool_stop_event.clear()
    os.makedirs(POOL_DIR, exist_ok=True)
    _pool_threads = []
    for i in range(POOL_WORKERS):
        t = threading.Thread(target=_pool_refill_loop, daemon=True, name=f"rootfs-pool-{i}")
        t.start()
        _pool_threads.append(t)
    log.debug("pool: started %d rootfs worker threads (target_size=%d)", POOL_WORKERS, POOL_TARGET_SIZE)


def stop_pool():
    global _pool_threads
    _pool_stop_event.set()
    for t in _pool_threads:
        t.join(timeout=5.0)
    _pool_threads = []
    if os.path.isdir(POOL_DIR):
        shutil.rmtree(POOL_DIR, ignore_errors=True)
