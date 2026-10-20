// Data hooks (TanStack Query) + the live event stream. Pages import from here only.
import { useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useEffect, useSyncExternalStore } from "react";
import { api, ApiError, mockMode, openSocket } from "./client";
import { normState, normVM } from "./normalize";
import type { FileEntry, FlintEvent, Health, Host, MetricSample, Platform, ProcessInfo, Quota, RawVM, Template, Token, VM } from "./types";

// ---------------- platform ----------------
export function usePlatform() {
  return useQuery<Platform>({
    queryKey: ["platform"],
    staleTime: 30_000,
    retry: (n, e) => !(e instanceof ApiError && e.status === 401) && n < 2,
    queryFn: async () => {
      const health = await api.get<Health>("/health");
      const mock = !!mockMode();
      if (health.role !== "control-plane") return { mode: "daemon", health, access: "local", mock };
      try { await api.get("/v1/hosts"); return { mode: "cluster", health, access: "admin", mock }; }
      catch (e) { if (e instanceof ApiError && e.status === 403) return { mode: "cluster", health, access: "service", mock }; throw e; }
    },
  });
}
export const useIsCluster = () => usePlatform().data?.mode === "cluster";
export const useIsAdmin = () => { const p = usePlatform().data; return p?.access === "admin"; };

// ---------------- VMs ----------------
export function useVms(tenant?: string) {
  return useQuery<VM[]>({
    queryKey: ["vms", tenant ?? ""],
    queryFn: async () => (await api.get<{ vms: RawVM[] }>("/vms", { query: { tenant } })).vms.map(normVM),
    refetchInterval: 10_000,
  });
}
export function useVm(id: string | undefined) {
  return useQuery<VM>({
    queryKey: ["vm", id],
    enabled: !!id,
    queryFn: async () => normVM((await api.get<{ vm: RawVM }>(`/vms/${id}`)).vm),
    refetchInterval: 10_000,
    retry: (n, e) => !(e instanceof ApiError && e.status === 404) && n < 2,
  });
}
export function useMetrics(id: string | undefined, enabled = true, every = 2000) {
  return useQuery<MetricSample[]>({
    queryKey: ["metrics", id],
    enabled: !!id && enabled,
    queryFn: async () => (await api.get<{ samples: MetricSample[] }>(`/vms/${id}/metrics`, { query: { window: 60 } })).samples,
    refetchInterval: every,
    retry: false,
  });
}
export function useProcesses(id: string | undefined, enabled = true) {
  return useQuery<ProcessInfo[]>({
    queryKey: ["processes", id], enabled: !!id && enabled, refetchInterval: 5000, retry: false,
    queryFn: async () => (await api.get<{ processes: ProcessInfo[] }>(`/vms/${id}/processes`)).processes ?? [],
  });
}
export function useFiles(id: string | undefined, path: string, enabled = true) {
  return useQuery<FileEntry[]>({
    queryKey: ["files", id, path], enabled: !!id && enabled, retry: false,
    queryFn: async () => (await api.get<{ entries: FileEntry[] }>(`/vms/${id}/files/list`, { query: { path } })).entries ?? [],
  });
}

// ---------------- templates / cluster ----------------
export function useTemplates() {
  return useQuery<Template[]>({ queryKey: ["templates"], queryFn: async () => (await api.get<{ templates: Template[] }>("/templates")).templates, refetchInterval: 15_000 });
}
export function useHosts(enabled = true) {
  return useQuery<Host[]>({ queryKey: ["hosts"], enabled, queryFn: async () => (await api.get<{ hosts: Host[] }>("/v1/hosts")).hosts, refetchInterval: 5000 });
}
export function useQuota(tenant: string | undefined, enabled = true) {
  return useQuery<Quota>({ queryKey: ["quota", tenant], enabled: !!tenant && enabled, queryFn: () => api.get<Quota>(`/v1/tenants/${tenant}/quota`) });
}
export function useTokens(enabled = true) {
  return useQuery<Token[]>({ queryKey: ["tokens"], enabled, queryFn: async () => (await api.get<{ tokens: Token[] }>("/v1/tokens")).tokens });
}
/** Tenants are derived: there is no tenants endpoint (API-CONTRACT gap 8). */
export function tenantsOf(vms: VM[] | undefined, tokens?: Token[]): string[] {
  const s = new Set<string>();
  vms?.forEach((v) => v.tenant && s.add(v.tenant));
  tokens?.forEach((t) => t.tenants.forEach((x) => s.add(x)));
  return [...s].sort();
}

// ---------------- actions ----------------
export interface CreateOpts { template: string; tenant?: string; name?: string; allowInternet?: boolean; timeoutSeconds?: number; timeoutPolicy?: "kill" | "pause"; labels?: Record<string, string> }
export async function createVM(o: CreateOpts, cluster: boolean): Promise<VM> {
  const labels = { ...(o.labels ?? {}), ...(o.name ? { name: o.name } : {}) };
  const res = cluster
    ? await api.post<{ vm: RawVM }>("/vms", { body: { tenant: o.tenant, template_id: o.template, labels, allow_internet_access: o.allowInternet ?? true }, idempotent: true })
    : await api.post<{ vm: RawVM }>("/vms", {
        query: { template_id: o.template, allow_internet_access: o.allowInternet ?? true },
        body: { ...(o.tenant ? { tenant: o.tenant } : {}), ...(o.timeoutSeconds ? { timeout_seconds: o.timeoutSeconds, timeout_policy: o.timeoutPolicy ?? "kill" } : {}) },
      });
  const vm = normVM(res.vm);
  if (cluster && o.timeoutSeconds) await api.patch(`/vms/${vm.id}`, { body: { timeout_seconds: o.timeoutSeconds, timeout_policy: o.timeoutPolicy ?? "kill" } });
  return vm;
}
export const vmActions = {
  pause: (id: string) => api.post(`/vms/${id}/pause`, { idempotent: true }),
  resume: (id: string) => api.post(`/vms/${id}/resume`, { idempotent: true }),
  kill: (id: string) => api.del(`/vms/${id}`, { idempotent: true }),
  checkpoint: (id: string) => api.post(`/vms/${id}/checkpoint`, { idempotent: true }),
  migrate: (id: string, host?: string) => api.post(`/vms/${id}/migrate`, { body: host ? { host_id: host } : {}, idempotent: true }),
  setTimeout: (id: string, seconds: number, policy: "kill" | "pause") => api.patch(`/vms/${id}`, { body: { timeout_seconds: seconds, timeout_policy: policy } }),
};
export function invalidateVMs(qc: QueryClient, id?: string) {
  qc.invalidateQueries({ queryKey: ["vms"] });
  if (id) qc.invalidateQueries({ queryKey: ["vm", id] });
}

// ---------------- live events ----------------
type Conn = "connecting" | "live" | "down";
interface EvState { conn: Conn; lastAt: number; events: (FlintEvent & { _k: string })[] }
let ev: EvState = { conn: "connecting", lastAt: 0, events: [] };
const listeners = new Set<() => void>();
const set = (p: Partial<EvState>) => { ev = { ...ev, ...p }; listeners.forEach((l) => l()); };
const sub = (l: () => void) => (listeners.add(l), () => listeners.delete(l));
export const useEvents = () => useSyncExternalStore(sub, () => ev);
let k = 0;

/** Mount once (App). Subscribes to /events, keeps the last 300, refreshes affected queries. */
export function useEventStream(enabled: boolean) {
  const qc = useQueryClient();
  useEffect(() => {
    if (!enabled) return;
    let sock: ReturnType<typeof openSocket> | null = null, retry = 0, stop = false, timer = 0;
    const pending = new Set<string>();
    const flush = () => {
      timer = 0;
      if (pending.has("vms")) qc.invalidateQueries({ queryKey: ["vms"] });
      if (pending.has("hosts")) qc.invalidateQueries({ queryKey: ["hosts"] });
      if (pending.has("templates")) qc.invalidateQueries({ queryKey: ["templates"] });
      pending.forEach((p) => p.startsWith("vm:") && qc.invalidateQueries({ queryKey: ["vm", p.slice(3)] }));
      pending.clear();
    };
    const connect = () => {
      set({ conn: ev.conn === "live" ? "connecting" : ev.conn });
      sock = openSocket("/events");
      sock.onopen = () => { retry = 0; set({ conn: "live", lastAt: Date.now() }); };
      sock.onmessage = (d) => {
        let e: FlintEvent; try { e = JSON.parse(String(d)); } catch { return; }
        if (e.type === "vm.state_changed" && e.to) e = { ...e, to: normState(String(e.to)) };
        set({ lastAt: Date.now(), events: [{ ...e, _k: `${++k}` }, ...ev.events].slice(0, 300) });
        if (e.type.startsWith("vm.")) { pending.add("vms"); if (e.vm_id) pending.add("vm:" + e.vm_id); }
        if (e.type.startsWith("host.") || e.type === "vm.moved") pending.add("hosts");
        if (e.type.startsWith("template.")) pending.add("templates");
        if (!timer) timer = window.setTimeout(flush, 250);
      };
      sock.onclose = () => {
        if (stop) return;
        set({ conn: "down" });
        retry = Math.min(retry + 1, 6);
        window.setTimeout(connect, 500 * 2 ** retry);
      };
    };
    connect();
    return () => { stop = true; sock?.close(); };
  }, [enabled, qc]);
}

// ---------------- recent VMs (sidebar) ----------------
const RECENT = "flint.recent";
export const recentVMs = (): string[] => { try { return JSON.parse(localStorage.getItem(RECENT) ?? "[]"); } catch { return []; } };
export function noteRecent(id: string) { localStorage.setItem(RECENT, JSON.stringify([id, ...recentVMs().filter((x) => x !== id)].slice(0, 4))); window.dispatchEvent(new Event("flint:recent")); }
