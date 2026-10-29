import type { RawVM, VM, VMState } from "./types";

// Daemon states: src/flint/core/types.py (SandboxState, _STATE_API_NAMES).
// Cluster states: cluster/internal/server/models.go (VMPending…).
const STATE: Record<string, VMState> = {
  starting: "starting", pending: "starting", started: "running", running: "running", paused: "paused",
  error: "error", deleting: "deleting", dead: "stopped", migrating: "migrating", recovering: "recovering", lost: "lost",
};

const warned = new Set<string>();

/** One mapping for REST `state` (Started…), events (`Running`…) and cluster `cluster_state`.
 *  A state missing from the map is "unknown" (and logged once), not a failure: a server newer
 *  than this UI must not paint healthy VMs red. */
export function normState(s: string | undefined | null): VMState {
  const k = (s ?? "").toLowerCase();
  const st = STATE[k];
  if (st) return st;
  if (!warned.has(k)) { warned.add(k); console.warn(`flint: unknown VM state ${JSON.stringify(s)}; shown as "unknown"`); }
  return "unknown";
}

export function normVM(r: RawVM): VM {
  const labels = r.labels ?? {};
  const shortId = r.vm_id.slice(0, 8);
  const name = labels.name || labels["flint.name"] || "";
  return {
    id: r.vm_id,
    shortId,
    name: name || shortId,
    hasName: !!name,
    tenant: r.tenant ?? "",
    template: r.template_id ?? "default",
    state: normState(r.cluster_state || r.state),
    host: r.host_id ?? "",
    backend: r.backend_kind ?? "",
    agentHealthy: !!r.agent_healthy,
    createdAt: r.created_at ?? null,
    readyMs: r.ready_time_ms ?? null,
    timings: r.timings ?? {},
    availability: r.availability ?? "",
    labels,
    vmset: r.vmset_id ?? "",
    timeoutSeconds: r.timeout_seconds ?? null,
    timeoutPolicy: r.timeout_policy ?? null,
    checkpointAt: r.checkpoint_at ?? null,
    error: r.error ?? "",
    networkPolicy: r.network_policy ?? null,
    logLines: r.log_lines ?? [],
    raw: r,
  };
}
