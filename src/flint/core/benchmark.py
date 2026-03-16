from __future__ import annotations

import time
import traceback

from .config import log
from ._boot import _boot_from_snapshot, _teardown_vm


def benchmark_vm(*, use_pool: bool = False, use_pyroute2: bool = False) -> dict:
    """Boot a VM, verify agent health, send one command, tear down. Returns timing results."""
    vm_id = None
    boot = None
    try:
        boot = _boot_from_snapshot(use_pool=use_pool, use_pyroute2=use_pyroute2)
        vm_id = boot.vm_id

        # Warmup via agent exec
        from .manager import SandboxManager
        t0 = time.monotonic()
        try:
            SandboxManager._agent_exec(boot.ns_name, boot.agent_url, ["echo", "benchmark"], timeout=5)
        except Exception:
            pass
        boot.timings["exec_command_ms"] = (time.monotonic() - t0) * 1000

        ready_time_ms = (time.monotonic() - boot.t_total) * 1000
        return {"vm_id": vm_id, "ready_time_ms": ready_time_ms, "timings": boot.timings,
                "success": True, "error": None}

    except Exception as exc:
        log.error("[%s] benchmark FAILED: %s", (vm_id or "?")[:8], traceback.format_exc())
        timings = boot.timings if boot else {}
        return {"vm_id": vm_id, "ready_time_ms": None, "timings": timings,
                "success": False, "error": str(exc)}

    finally:
        if boot:
            _teardown_vm(boot.process, boot.ns_name, boot.vm_dir)
