"""Background pool of per-VM disk artifacts, prepared ahead of create().

Each entry is a directory holding private, writable copies of a template's
files (always ``rootfs.ext4``; Firecracker also wants a base for its first diff
snapshot). Copies are sparse and fsynced here, off the create/pause path. The
pool lives on the same filesystem as the VM directories so a claimed entry is
moved into place with a hard link.
"""

import os
import shutil
import threading
import uuid
from typing import Callable

from . import config
from .config import log, DEFAULT_TEMPLATE_ID
from ._artifacts import fsync_path, sparse_copy

# template_id -> {dest_name: source_path}; None when the template isn't ready.
PoolSources = Callable[[str], "dict[str, str] | None"]

_pool_lock = threading.Lock()
_pool_entries: list[dict] = []
_pool_stop_event = threading.Event()
_pool_threads: list[threading.Thread] = []
_pool_dir: str = config.POOL_DIR
_pool_sources: PoolSources | None = None
_pool_dir_ino: int = 0  # the pool dir's inode at start, to tell whether it was replaced


TEMPLATE_POOL_SIZE = 2  # pool size for non-default templates


def _fingerprint(sources: dict[str, str]) -> tuple:
    """Identify the exact source files, so entries from a rebuilt template are never used."""
    return tuple(sorted((name, os.stat(src).st_ino, os.stat(src).st_mtime_ns) for name, src in sources.items()))


def _copy_one_to_pool(sources: dict[str, str], template_id: str) -> str | None:
    pool_id = str(uuid.uuid4())
    pool_entry_dir = f"{_pool_dir}/{pool_id}"

    entry = {
        "id": pool_id, "dir_path": pool_entry_dir, "template_id": template_id,
        "state": "copying", "fingerprint": _fingerprint(sources),
    }
    with _pool_lock:
        _pool_entries.append(entry)

    try:
        os.makedirs(pool_entry_dir, exist_ok=True)
        for name, src in sources.items():
            dest = f"{pool_entry_dir}/{name}"
            sparse_copy(src, dest)
            fsync_path(dest)
        with _pool_lock:
            entry["state"] = "ready"
        return pool_id
    except Exception:
        log.exception("pool: failed to create entry %s", pool_id[:8])
        shutil.rmtree(pool_entry_dir, ignore_errors=True)
        with _pool_lock:
            _pool_entries.remove(entry)
        return None


def _count(template_id: str) -> int:
    with _pool_lock:
        return sum(1 for e in _pool_entries if e["template_id"] == template_id and e["state"] in ("ready", "copying"))


def _pool_refill_loop():
    from ._template_registry import registered_template_ids

    while not _pool_stop_event.is_set():
        try:
            filled = False
            template_ids = [DEFAULT_TEMPLATE_ID] + [t for t in registered_template_ids() if t != DEFAULT_TEMPLATE_ID]
            for tid in template_ids:
                target = config.POOL_TARGET_SIZE if tid == DEFAULT_TEMPLATE_ID else TEMPLATE_POOL_SIZE
                if _count(tid) >= target:
                    continue
                sources = _pool_sources(tid) if _pool_sources else None
                if not sources:
                    continue
                _copy_one_to_pool(sources, tid)
                filled = True

            if not filled:
                _pool_stop_event.wait(0.02)
        except Exception:
            log.exception("pool: refill loop error")
            _pool_stop_event.wait(1.0)


def _claim_pool_entry(template_id: str) -> str | None:
    """Claim a pool entry. Returns pool entry dir path, or None on miss. Caller owns cleanup."""
    sources = _pool_sources(template_id) if _pool_sources else None
    try:
        fingerprint = _fingerprint(sources) if sources else None
    except OSError:
        fingerprint = None
    with _pool_lock:
        ready = [e for e in _pool_entries if e["state"] == "ready" and e["template_id"] == template_id]
        stale = [e for e in ready if e["fingerprint"] != fingerprint]
        match = next((e for e in ready if e["fingerprint"] == fingerprint), None)
        taken = stale + ([match] if match else [])
        for e in taken:
            _pool_entries.remove(e)
        remaining = len(ready) - len(taken)
        copying_count = sum(1 for e in _pool_entries if e["state"] == "copying")
    for e in stale:
        log.debug("pool: dropping stale entry %s for %s", e["id"][:8], template_id)
        shutil.rmtree(e["dir_path"], ignore_errors=True)
    if not match:
        log.debug("pool: miss for %s (ready=%d, copying=%d)", template_id, remaining, copying_count)
        return None
    missing = [n for n in (sources or {}) if not os.path.exists(f"{match['dir_path']}/{n}")]
    if missing:
        report_lost(match["dir_path"], missing)
        shutil.rmtree(match["dir_path"], ignore_errors=True)
        return None
    log.debug("pool: claimed %s (%d remaining)", match["id"][:8], remaining)
    return match["dir_path"]


def report_lost(entry_dir: str, missing: list[str]) -> None:
    """A pool entry lost files it had. Callers fall back to copying from the
    template; this logs what the pool directory looks like, to find out why."""
    try:
        st = os.stat(_pool_dir)
        where = f"pool dir {'replaced' if st.st_ino != _pool_dir_ino else 'same'} (inode {_pool_dir_ino} -> {st.st_ino}), " \
                f"{len(os.listdir(_pool_dir))} entries on disk"
    except OSError as e:
        where = f"pool dir unreadable: {e}"
    with _pool_lock:
        tracked = len(_pool_entries)
    try:
        with open("/proc/self/mountinfo") as f:
            parent = os.path.dirname(_pool_dir)
            mounts = [line.split()[4] for line in f if line.split()[4].startswith(parent)]
    except OSError:
        mounts = []
    log.error("pool: entry %s lost %s; %s, %d tracked; mounts under %s: %s",
              os.path.basename(entry_dir)[:8], missing, where, tracked, os.path.dirname(_pool_dir), mounts or "none")


def start_pool(pool_dir: str, sources: PoolSources) -> None:
    """Start refill workers writing entries under ``pool_dir``.

    ``pool_dir`` must be on the same filesystem as the VM directories the
    entries are claimed into.
    """
    global _pool_threads, _pool_dir, _pool_sources
    _pool_dir = pool_dir
    _pool_sources = sources
    _pool_stop_event.clear()
    # Entries from a previous daemon aren't tracked; start clean.
    shutil.rmtree(_pool_dir, ignore_errors=True)
    os.makedirs(_pool_dir, exist_ok=True)
    global _pool_dir_ino
    _pool_dir_ino = os.stat(_pool_dir).st_ino
    _pool_threads = []
    for i in range(config.POOL_WORKERS):
        t = threading.Thread(target=_pool_refill_loop, daemon=True, name=f"rootfs-pool-{i}")
        t.start()
        _pool_threads.append(t)
    log.debug("pool: started %d workers in %s (target_size=%d)", config.POOL_WORKERS, _pool_dir, config.POOL_TARGET_SIZE)


def stop_pool():
    global _pool_threads
    _pool_stop_event.set()
    for t in _pool_threads:
        t.join(timeout=5.0)
    _pool_threads = []
    with _pool_lock:
        _pool_entries.clear()
    if os.path.isdir(_pool_dir):
        shutil.rmtree(_pool_dir, ignore_errors=True)
