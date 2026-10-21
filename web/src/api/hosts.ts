// Host / tenant admin actions and one-off reads used by the Hosts and Tenants pages.
import { useQuery } from "@tanstack/react-query";
import { api, ApiError } from "./client";
import type { FlintEvent, Host } from "./types";

export const errMsg = (e: unknown): string => (e instanceof ApiError ? e.detail || `HTTP ${e.status}` : e instanceof Error ? e.message : String(e));

export const hostActions = {
  cordon: (id: string) => api.post(`/v1/hosts/${id}/cordon`),
  uncordon: (id: string) => api.post(`/v1/hosts/${id}/uncordon`),
  drain: (id: string) => api.post(`/v1/hosts/${id}/drain`),
  setMaxVms: (id: string, n: number) => api.patch(`/v1/hosts/${id}`, { body: { max_vms: n } }),
  remove: (id: string, force: boolean) => api.del(`/v1/hosts/${id}`, { query: { force: force || undefined } }),
};

/** VM cap the scheduler uses: the override when it is set and below the host's own max. */
export const effectiveMax = (h: Host): number => {
  const m = h.capacity?.max_vms ?? 0, o = h.max_vms_override ?? 0;
  return o > 0 && (m <= 0 || o < m) ? o : m;
};

/** Cluster event history, fetched once on mount (live events come from useEvents). */
export function useEventHistory() {
  return useQuery<FlintEvent[]>({
    queryKey: ["events-history"], staleTime: Infinity, gcTime: 0, retry: false, refetchOnWindowFocus: false,
    queryFn: async () => (await api.get<{ events: FlintEvent[] }>("/v1/events")).events ?? [],
  });
}

export const tenantActions = {
  setQuota: (tenant: string, maxVms: number) => api.put(`/v1/tenants/${tenant}/quota`, { body: { max_vms: maxVms } }),
  createToken: (tenant: string, name: string) =>
    api.post<{ id: string; token: string; kind: string; tenants: string[] }>("/v1/tokens", { body: { kind: "service", name, tenants: [tenant] } }),
  revokeToken: (id: string) => api.del(`/v1/tokens/${id}`),
};
