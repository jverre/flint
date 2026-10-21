import type { RawVM, VM, VMState } from "./types";

const STATE: Record<string, VMState> = {
  starting: "starting", pending: "starting", started: "running", running: "running", paused: "paused",
  error: "error", dead: "stopped", migrating: "migrating", recovering: "recovering", lost: "lost",
};

/** One mapping for REST `state` (Started…), events (`Running`…) and cluster `cluster_state`. */
export function normState(s: string | undefined | null): VMState {
  return STATE[(s ?? "").toLowerCase()] ?? "error";
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

/** Phase names from `timings` in a stable, human order (backend-specific keys pass through). */
export function orderedTimings(t: Record<string, number>): [string, number][] {
  return Object.entries(t).filter(([, v]) => typeof v === "number").sort((a, b) => b[1] - a[1] && 0);
}
