// In-browser mock of the daemon + control-plane API (same routes, same shapes as the real servers:
// see the note at the top of types.ts; type mock replies with those types so they can't drift).
// Used with `?mock` (cluster) or `?mock=daemon` for demos, screenshots and developing without
// VMs. Not used against a real server.
import type { Sock } from "./client";
import type { FileEntry, FlintEvent, Host, ProcessInfo, RawVM, Template, Token } from "./types";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const now = () => Date.now() / 1000;
const rnd = (a: number, b: number) => a + Math.random() * (b - a);
const uuid = () => crypto.randomUUID();
const mode = () => (sessionStorage.getItem("flint.mock") === "daemon" ? "daemon" : "cluster");

// ---------------- dataset ----------------
const HOSTS: Host[] = [
  ["h-ams-1", "ams-a", 64, 262144], ["h-ams-2", "ams-b", 64, 262144], ["h-fra-1", "fra-a", 96, 393216], ["h-fra-2", "fra-b", 96, 393216],
].map(([id, zone, vcpus, mem], i) => ({
  id: id as string, name: id as string, zone: zone as string, labels: {}, backend: "linux-firecracker", host_class: "epyc-9454",
  state: "ready", cordoned: false,
  capacity: { max_vms: 48, vcpus: vcpus as number, mem_total_mb: mem as number, mem_available_mb: (mem as number) * 0.9, disk_free_mb: 812000, netns_free: 200 },
  cached_snapshots: [], templates: [], version: i === 3 ? "0.9.1" : "0.9.2",
  last_heartbeat_ms: Date.now(), state_since_ms: Date.now() - 864e5 * 3, max_vms_override: 0, created_at_ms: Date.now() - 864e5 * 40, active_vms: 0,
}));

const TEMPLATES: Template[] = [
  { template_id: "default", name: "Default (Alpine)", status: "ready", created_at: now() - 864e5 / 1000 * 20 },
  { template_id: "python-3-12", name: "python-3.12", image: "index.docker.io/library/python:3.12-slim", digest: "sha256:ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12", status: "ready", created_at: now() - 6 * 86400, rootfs_size_mb: 412 },
  { template_id: "node-22", name: "node-22", image: "index.docker.io/library/node:22-slim", digest: "sha256:ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12", status: "ready", created_at: now() - 9 * 86400, rootfs_size_mb: 298 },
  { template_id: "ubuntu-24-04", name: "ubuntu-24.04", image: "index.docker.io/library/ubuntu:24.04", digest: "sha256:ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12", status: "ready", created_at: now() - 20 * 86400, rootfs_size_mb: 611 },
];

const SEED: [string, string, string, string, number, string][] = [
  // name, tenant, host, template, age minutes, state
  ["agent-runner-12", "lumen-agents", "h-ams-1", "python-3-12", 192, "running"],
  ["ci-job-48213", "acme-ci", "h-fra-1", "node-22", 14, "running"],
  ["nb-kernel-ines", "orbit-notebooks", "h-ams-2", "python-3-12", 1680, "running"],
  ["agent-runner-13", "lumen-agents", "h-ams-1", "python-3-12", 41, "paused"],
  ["ci-job-48214", "acme-ci", "h-ams-2", "node-22", 6, "running"],
  ["sandbox-try", "default", "h-fra-1", "default", 2, "running"],
  ["nb-kernel-tom", "orbit-notebooks", "h-fra-1", "ubuntu-24-04", 302, "running"],
  ["agent-runner-14", "lumen-agents", "h-fra-2", "python-3-12", 58, "running"],
  ["agent-runner-15", "lumen-agents", "h-fra-2", "python-3-12", 12, "running"],
  ["ci-job-48215", "acme-ci", "h-ams-1", "node-22", 3, "running"],
  ["nb-kernel-sara", "orbit-notebooks", "h-fra-2", "python-3-12", 2940, "running"],
  ["agent-runner-16", "lumen-agents", "h-ams-2", "python-3-12", 1, "error"],
  ["nb-kernel-ali", "orbit-notebooks", "h-ams-1", "ubuntu-24-04", 460, "paused"],
  ["ci-job-48171", "acme-ci", "h-ams-1", "node-22", 185, "running"],
  ["ci-job-48177", "acme-ci", "h-ams-2", "node-22", 161, "running"],
  ["ci-job-48180", "acme-ci", "h-fra-1", "node-22", 132, "running"],
  ["ci-job-48184", "acme-ci", "h-fra-1", "node-22", 118, "running"],
  ["ci-job-48188", "acme-ci", "h-fra-2", "node-22", 100, "running"],
  ["ci-job-48190", "acme-ci", "h-ams-2", "node-22", 82, "running"],
  ["nb-kernel-raj", "orbit-notebooks", "h-ams-2", "python-3-12", 610, "running"],
];

function phases(): Record<string, number> {
  const t = { placing: rnd(4, 9), jail: rnd(24, 38), netns: rnd(1.5, 3), restore: rnd(70, 110), agent_connect: rnd(30, 52) };
  return Object.fromEntries(Object.entries(t).map(([k, v]) => [k, Math.round(v * 10) / 10]));
}
const total = (t: Record<string, number>) => Math.round(Object.values(t).reduce((a, b) => a + b, 0) * 10) / 10;

interface MVM extends RawVM { _cpu: number; _rssMiB: number }
const STATE_API: Record<string, string> = { running: "Started", paused: "Paused", error: "Error", starting: "Starting", migrating: "Migrating" };
const VMS: MVM[] = [];
function mkVM(name: string, tenant: string, host: string, tpl: string, ageMin: number, st: string, labels: Record<string, string> = {}): MVM {
  const t = phases();
  return {
    vm_id: uuid(), state: STATE_API[st], cluster_state: st === "running" ? "running" : st, template_id: tpl, backend_kind: "linux-firecracker",
    agent_healthy: st === "running", created_at: now() - ageMin * 60, boot_time_ms: total(t) - t.agent_connect, ready_time_ms: total(t), timings: t,
    log_lines: [], network_policy: tenant === "lumen-agents" ? { allow: { "api.openai.com": [{ transform: [{ headers: { Authorization: "sk-p***9f2a" } }] }] } } : null,
    timeout_seconds: null, timeout_policy: null, tenant, availability: "ephemeral", host_id: host, labels: { name, ...labels },
    error: st === "error" ? "guest agent did not answer within 5000 ms" : undefined,
    _cpu: rnd(5, 80), _rssMiB: rnd(120, 900), pid: 4000 + VMS.length,
  };
}
SEED.forEach(([n, t, h, tpl, age, st]) => VMS.push(mkVM(n, t, h, tpl, age, st)));
VMS[1]._rssMiB = 1930; VMS[1]._cpu = 92;

const TOKENS: Token[] = [
  { id: "tk_ops", kind: "admin", tenants: [], name: "ops-admin", created_at: now() - 86400 * 90 },
  { id: "tk_lumen", kind: "service", tenants: ["lumen-agents"], name: "lumen-prod-runner", created_at: now() - 86400 * 30 },
  { id: "tk_acme", kind: "service", tenants: ["acme-ci"], name: "acme-ci-runner", created_at: now() - 86400 * 12 },
];
const QUOTA: Record<string, number> = { "acme-ci": 32, "lumen-agents": 24, "orbit-notebooks": 16, default: 8 };

// ---------------- events ----------------
const EVENTS: (FlintEvent & { seq: number })[] = [];
let seq = 0;
const subs = new Set<(e: FlintEvent) => void>();
function emit(e: Omit<FlintEvent, "ts"> & { ts?: number }) {
  const ev = { ts: now(), ...e } as FlintEvent;
  EVENTS.push({ ...ev, seq: ++seq }); if (EVENTS.length > 2000) EVENTS.shift();
  subs.forEach((f) => f(ev));
  if (ev.vm_id) pushLog(ev.vm_id, ev.type.replace("vm.", "VM ") + (ev.to ? ` → ${ev.to}` : ""));
}
const LOGS = new Map<string, string[]>();
const logSubs = new Map<string, Set<(l: string) => void>>();
function pushLog(id: string, msg: string) {
  const d = new Date(); const line = `[${d.toTimeString().slice(0, 8)}] ${msg}`;
  const a = LOGS.get(id) ?? []; a.push(line); if (a.length > 1000) a.shift(); LOGS.set(id, a);
  logSubs.get(id)?.forEach((f) => f(line));
}
VMS.forEach((v) => {
  pushLog(v.vm_id, `VM created from template '${v.template_id}' in ${v.ready_time_ms} ms`);
  Object.entries(v.timings ?? {}).forEach(([k, ms]) => pushLog(v.vm_id, `  timing.${k} = ${ms} ms`));
  if (v.cluster_state === "paused") pushLog(v.vm_id, "VM paused");
});

// history for /v1/events: each seeded VM's start (and pause), oldest first
VMS.slice().sort((a, b) => (a.created_at ?? 0) - (b.created_at ?? 0)).forEach((v) => {
  EVENTS.push({ type: "vm.created", ts: v.created_at ?? now(), vm_id: v.vm_id, vm: v, host_id: v.host_id, tenant: v.tenant, seq: ++seq });
  if (v.cluster_state === "paused") EVENTS.push({ type: "vm.paused", ts: (v.created_at ?? now()) + 600, vm_id: v.vm_id, host_id: v.host_id, tenant: v.tenant, seq: ++seq });
});
EVENTS.push({ type: "host.ready", ts: now() - 3600, host_id: "h-fra-2", previous: "joining", seq: ++seq });

// background: heartbeats, a little churn
setInterval(() => { HOSTS.forEach((h) => (h.last_heartbeat_ms = Date.now() - rnd(0, 1800))); }, 1000);

// ---------------- helpers ----------------
const json = (x: unknown, status = 200) => new Response(JSON.stringify(x), { status, headers: { "Content-Type": "application/json" } });
const err = (status: number, detail: string) => json({ detail }, status);
const pub = (v: MVM): RawVM => {
  const { _cpu, _rssMiB, ...r } = v; void _cpu; void _rssMiB;
  if (mode() === "daemon") { const d = { ...r }; delete d.host_id; delete d.labels; delete d.cluster_state; return { ...d, tenant: "", availability: "" }; }
  return r;
};
const find = (id: string) => VMS.find((v) => v.vm_id === id);
const hostSlots = new Map<string, number>();
function pickHost(): string {
  const ready = HOSTS.filter((h) => h.state === "ready" && !h.cordoned);
  ready.sort((a, b) => VMS.filter((v) => v.host_id === a.id).length - VMS.filter((v) => v.host_id === b.id).length);
  return ready[0]?.id ?? "";
}
async function slot(host: string) {
  while ((hostSlots.get(host) ?? 0) >= 4) await sleep(10);
  hostSlots.set(host, (hostSlots.get(host) ?? 0) + 1);
}
const unslot = (host: string) => hostSlots.set(host, Math.max(0, (hostSlots.get(host) ?? 1) - 1));

// ---------------- router ----------------
type H = (m: RegExpMatchArray, q: URLSearchParams, body: unknown, raw: BodyInit | undefined) => Promise<Response> | Response;
const routes: [string, RegExp, H][] = [];
const on = (method: string, re: RegExp, h: H) => routes.push([method, re, h]);

on("GET", /^\/health$/, () => mode() === "daemon"
  ? json({ status: "ok", golden_snapshot_ready: true, backend_kind: "linux-firecracker", storage_backend: "local", storage_healthy: true })
  : json({ status: "ok", role: "control-plane", replica: "r-1", leader: true, hosts: HOSTS.length, hosts_ready: HOSTS.filter((h) => h.state === "ready").length, backend_kind: "cluster", golden_snapshot_ready: true }));

on("GET", /^\/vms$/, (_m, q) => json({ vms: VMS.filter((v) => !q.get("tenant") || v.tenant === q.get("tenant")).map(pub) }));
on("GET", /^\/vms\/([^/]+)$/, (m) => { const v = find(m[1]); return v ? json({ vm: pub(v) }) : err(404, "VM not found"); });
on("POST", /^\/vms$/, async (_m, q, b) => {
  const body = (b ?? {}) as Record<string, unknown>;
  const tenant = String(body.tenant ?? q.get("tenant") ?? (mode() === "daemon" ? "" : ""));
  if (mode() === "cluster" && !tenant) return err(400, "tenant is required (query or body)");
  const tpl = String(body.template_id ?? q.get("template_id") ?? "default");
  if (!TEMPLATES.find((t) => t.template_id === tpl && t.status === "ready")) return err(404, `template not found: ${tpl}`);
  if (mode() === "cluster" && QUOTA[tenant] && VMS.filter((v) => v.tenant === tenant).length >= QUOTA[tenant]) {
    return err(429, `tenant ${tenant} is at its quota: ${QUOTA[tenant]} of ${QUOTA[tenant]} VMs, 1 more requested`);
  }
  const host = mode() === "daemon" ? "local" : pickHost();
  if (!host) return err(503, "capacity_exhausted: no ready host");
  const labels = (body.labels ?? {}) as Record<string, string>;
  const v = mkVM(labels.name ?? "", tenant, host, tpl, 0, "starting", labels);
  if (!labels.name) delete v.labels!.name;
  v.state = "Starting"; v.cluster_state = "pending"; v.agent_healthy = false;
  VMS.unshift(v);
  await slot(host);
  const t = v.timings!; const ms = total(t) * rnd(0.95, 1.08);
  await sleep(ms); unslot(host);
  v.state = "Started"; v.cluster_state = "running"; v.agent_healthy = true; v.ready_time_ms = Math.round(ms * 10) / 10; v._rssMiB = rnd(80, 160); v._cpu = rnd(2, 20);
  if (body.timeout_seconds) v.timeout_seconds = Number(body.timeout_seconds);
  emit({ type: "vm.created", vm_id: v.vm_id, vm: pub(v), host_id: v.host_id, tenant: v.tenant });
  return json({ vm: pub(v) });
});
on("DELETE", /^\/vms\/([^/]+)$/, async (m) => {
  if (!find(m[1])) return err(404, "VM not found");
  await sleep(rnd(40, 110));
  const i = VMS.findIndex((v) => v.vm_id === m[1]); if (i < 0) return err(404, "VM not found");
  const v = VMS[i]; VMS.splice(i, 1);
  emit({ type: "vm.deleted", vm_id: v.vm_id, host_id: v.host_id, tenant: v.tenant }); return json({ ok: true });
});
on("POST", /^\/vms\/([^/]+)\/pause$/, async (m) => {
  const v = find(m[1]); if (!v) return err(404, "VM not found"); if (v.cluster_state !== "running") return err(409, `VM is ${v.state}`);
  await sleep(rnd(90, 160)); v.state = "Paused"; v.cluster_state = "paused"; v.agent_healthy = false;
  emit({ type: "vm.paused", vm_id: v.vm_id, tenant: v.tenant, host_id: v.host_id }); return json({ ok: true });
});
on("POST", /^\/vms\/([^/]+)\/resume$/, async (m) => {
  const v = find(m[1]); if (!v) return err(404, "VM not found"); if (v.cluster_state !== "paused") return err(500, "Resume failed: VM is not paused");
  await sleep(rnd(120, 200)); v.state = "Started"; v.cluster_state = "running"; v.agent_healthy = true;
  emit({ type: "vm.resumed", vm_id: v.vm_id, vm: pub(v), tenant: v.tenant, host_id: v.host_id }); return json({ vm: pub(v) });
});
on("POST", /^\/vms\/([^/]+)\/checkpoint$/, async (m) => {
  if (mode() === "daemon") return err(400, "checkpoint is only available in a cluster");
  const v = find(m[1]); if (!v) return err(404, "VM not found"); if (v.cluster_state !== "running") return err(409, "VM is not running");
  await sleep(rnd(180, 260)); v.checkpoint_at = now();
  emit({ type: "vm.checkpointed", vm_id: v.vm_id, snapshot_id: "snap-" + uuid().slice(0, 8), bytes: Math.round(v._rssMiB * 1048576), tenant: v.tenant }); return json({ vm: pub(v), snapshot_id: "snap" });
});
on("POST", /^\/vms\/([^/]+)\/migrate$/, async (m, _q, b) => {
  const v = find(m[1]); if (!v) return err(404, "VM not found");
  const to = String((b as { host_id?: string })?.host_id || HOSTS.find((h) => h.id !== v.host_id && h.state === "ready" && !h.cordoned)?.id || "");
  await sleep(rnd(600, 1400)); const from = v.host_id; v.host_id = to;
  emit({ type: "vm.moved", vm_id: v.vm_id, from_host: from, to_host: to, reason: "requested", tenant: v.tenant }); return json({ vm: pub(v) });
});
on("PATCH", /^\/vms\/([^/]+)$/, (m, _q, b) => {
  const v = find(m[1]); if (!v) return err(404, "VM not found"); const p = b as { timeout_seconds?: number; timeout_policy?: "kill" | "pause" };
  v.timeout_seconds = p.timeout_seconds ?? null; v.timeout_policy = p.timeout_policy ?? "kill"; return json({ ok: true });
});
on("GET", /^\/vms\/([^/]+)\/metrics$/, (m, q) => {
  const v = find(m[1]); if (!v || v.cluster_state !== "running") return err(404, "VM not found");
  const n = Math.min(60, Number(q.get("window") ?? 60)), t0 = now();
  const seed = v.vm_id.charCodeAt(0);
  const samples = Array.from({ length: n }, (_, i) => {
    const k = n - 1 - i;
    return { ts: t0 - k, cpu_percent: Math.max(0, Math.round((v._cpu + Math.sin((t0 - k) / 7 + seed) * 12 + rnd(-6, 6)) * 100) / 100), rss_bytes: Math.round((v._rssMiB + Math.sin((t0 - k) / 13 + seed) * 6) * 1048576) };
  });
  return json({ samples });
});
on("GET", /^\/vms\/([^/]+)\/network-policy$/, (m) => { const v = find(m[1]); return v ? json({ network_policy: v.network_policy }) : err(404, "VM not found"); });
on("PUT", /^\/vms\/([^/]+)\/network-policy$/, (m, _q, b) => { const v = find(m[1]); if (!v) return err(404, "VM not found"); v.network_policy = b as RawVM["network_policy"]; return json({ ok: true }); });
on("GET", /^\/vms\/([^/]+)\/processes$/, (m) => {
  const v = find(m[1]); if (!v) return err(404, "VM not found");
  // What flintd lists: the processes it started (not init or itself), forgotten once they exit.
  const processes: ProcessInfo[] = [
    { pid: 211, tag: "", pty: true, cmd: ["/bin/sh"], exited: false },
    { pid: 4127, tag: "agent", pty: false, cmd: ["python", "main.py", "--resume"], exited: false },
  ];
  return json({ processes });
});
const FS: Record<string, FileEntry[]> = {
  "/workspace": [{ name: "main.py", size: 4812, is_dir: false, mode: "0644" }, { name: "agent.toml", size: 611, is_dir: false, mode: "0644" }, { name: "requirements.txt", size: 142, is_dir: false, mode: "0644" }, { name: "out", size: 0, is_dir: true, mode: "0755" }],
  "/workspace/out": [{ name: "report-0411.md", size: 20480, is_dir: false, mode: "0644" }],
  "/": [{ name: "workspace", size: 0, is_dir: true, mode: "0755" }, { name: "etc", size: 0, is_dir: true, mode: "0755" }, { name: "tmp", size: 0, is_dir: true, mode: "1777" }],
};
on("GET", /^\/vms\/([^/]+)\/files\/list$/, (_m, q) => { const p = q.get("path") ?? "/"; return FS[p] ? json({ entries: FS[p] }) : json({ error: `stat ${p}: no such file or directory` }, 404); });
on("GET", /^\/vms\/([^/]+)\/files\/stat$/, (_m, q) => json({ path: q.get("path"), size: 611, is_dir: false, mode: "0644" }));
on("GET", /^\/vms\/([^/]+)\/files$/, (_m, q) => new Response(`# ${q.get("path")}\n[agent]\nmodel = "gpt-5"\nmax_steps = 80\n`, { status: 200 }));
on("POST", /^\/vms\/([^/]+)\/files$/, (_m, q) => { const p = q.get("path") ?? ""; const dir = p.slice(0, p.lastIndexOf("/")) || "/"; (FS[dir] ??= []).push({ name: p.slice(p.lastIndexOf("/") + 1), size: 1024, is_dir: false, mode: "0644" }); return json({ ok: true }); });
on("POST", /^\/vms\/([^/]+)\/files\/mkdir$/, (_m, q) => { const p = q.get("path") ?? ""; const dir = p.slice(0, p.lastIndexOf("/")) || "/"; (FS[dir] ??= []).push({ name: p.slice(p.lastIndexOf("/") + 1), size: 0, is_dir: true, mode: "0755" }); FS[p] ??= []; return json({ ok: true }); });
on("DELETE", /^\/vms\/([^/]+)\/files$/, (_m, q) => { const p = q.get("path") ?? ""; const dir = p.slice(0, p.lastIndexOf("/")) || "/"; FS[dir] = (FS[dir] ?? []).filter((e) => e.name !== p.slice(p.lastIndexOf("/") + 1)); return json({ ok: true }); });
on("POST", /^\/vms\/([^/]+)\/exec$/, (_m, _q, b) => { const c = ((b as { cmd?: string[] })?.cmd ?? []).join(" "); return json({ stdout: c.includes("uname") ? "Linux flint 6.1.190-flint #1 SMP x86_64 GNU/Linux\n" : "", stderr: "", exit_code: 0 }); });

on("GET", /^\/templates$/, () => json({ templates: mode() === "daemon" ? TEMPLATES.map((t) => ({ ...t, artifacts: { "linux-firecracker": { status: t.status } } })) : TEMPLATES }));
on("GET", /^\/templates\/([^/]+)$/, (m) => { const t = TEMPLATES.find((x) => x.template_id === m[1]); return t ? json({ template: { ...t, host_classes: ["epyc-9454"] } }) : err(404, "Template not found"); });
on("POST", /^\/templates$/, (_m, _q, b) => {
  const p = b as { name: string; image: string }; const id = p.name.toLowerCase().replace(/[^a-z0-9]+/g, "-");
  const t: Template = { template_id: id, name: p.name, status: "building", created_at: now(), build_host: "h-ams-1" }; TEMPLATES.push(t);
  setTimeout(() => { t.status = "ready"; t.image = p.image; t.digest = "sha256:" + "0".repeat(64); emit({ type: "template.ready", template_id: id }); }, 8000);
  return json({ template_id: id, status: "building", build_host: "h-ams-1" });
});
on("DELETE", /^\/templates\/([^/]+)$/, (m) => { const i = TEMPLATES.findIndex((t) => t.template_id === m[1]); if (m[1] === "default") return err(400, "cannot delete the default template"); if (i >= 0) TEMPLATES.splice(i, 1); return json({ ok: true }); });

// cluster admin
const clusterOnly = (h: H): H => (m, q, b, r) => (mode() === "daemon" ? err(404, "Not Found") : h(m, q, b, r));
const hostDict = (h: Host) => ({ ...h, active_vms: VMS.filter((v) => v.host_id === h.id && v.cluster_state !== "paused").length });
on("GET", /^\/v1\/hosts$/, clusterOnly(() => json({ hosts: HOSTS.map(hostDict) })));
on("GET", /^\/v1\/hosts\/([^/]+)$/, clusterOnly((m) => { const h = HOSTS.find((x) => x.id === m[1]); return h ? json({ host: { ...hostDict(h), vms: VMS.filter((v) => v.host_id === h.id).map((v) => v.vm_id) } }) : err(404, "host not found"); }));
on("POST", /^\/v1\/hosts\/([^/]+)\/cordon$/, clusterOnly((m) => { const h = HOSTS.find((x) => x.id === m[1]); if (!h) return err(404, "host not found"); h.cordoned = true; return json({ ok: true }); }));
on("POST", /^\/v1\/hosts\/([^/]+)\/uncordon$/, clusterOnly((m) => {
  const h = HOSTS.find((x) => x.id === m[1]); if (!h) return err(404, "host not found");
  h.cordoned = false; if (h.state === "draining" || h.state === "drained") { h.state = "ready"; emit({ type: "host.ready", host_id: h.id, previous: "drained" }); }
  return json({ ok: true });
}));
on("POST", /^\/v1\/hosts\/([^/]+)\/drain$/, clusterOnly((m) => {
  const h = HOSTS.find((x) => x.id === m[1]); if (!h) return err(404, "host not found");
  h.cordoned = true; h.state = "draining"; h.state_since_ms = Date.now(); emit({ type: "host.draining", host_id: h.id });
  (async () => {
    for (const v of VMS.filter((x) => x.host_id === h.id && x.cluster_state === "running")) {
      await sleep(rnd(900, 1600)); const to = pickHost(); const from = v.host_id; v.host_id = to;
      emit({ type: "vm.moved", vm_id: v.vm_id, from_host: from, to_host: to, reason: "drain", tenant: v.tenant });
    }
    VMS.filter((x) => x.host_id === h.id).forEach((v) => (v.host_id = ""));
    await sleep(300); h.state = "drained"; h.state_since_ms = Date.now(); emit({ type: "host.drained", host_id: h.id });
  })();
  return json({ ok: true, state: "draining" });
}));
on("PATCH", /^\/v1\/hosts\/([^/]+)$/, clusterOnly((m, _q, b) => { const h = HOSTS.find((x) => x.id === m[1]); if (!h) return err(404, "host not found"); h.max_vms_override = Number((b as { max_vms: number }).max_vms); return json({ host: hostDict(h) }); }));
on("DELETE", /^\/v1\/hosts\/([^/]+)$/, clusterOnly((m, q) => {
  const i = HOSTS.findIndex((x) => x.id === m[1]); if (i < 0) return err(404, "host not found");
  const n = VMS.filter((v) => v.host_id === m[1]).length;
  if (n && q.get("force") !== "true") return err(409, `host ${m[1]} still runs ${n} VMs; drain it first or pass force=true`);
  VMS.filter((v) => v.host_id === m[1]).forEach((v) => { v.cluster_state = "lost"; v.state = "Lost"; });
  HOSTS.splice(i, 1); emit({ type: "host.removed", host_id: m[1] }); return json({ ok: true });
}));
on("GET", /^\/v1\/tenants\/([^/]+)\/quota$/, clusterOnly((m) => json({ tenant: m[1], max_vms: QUOTA[m[1]] ?? 0, vms: VMS.filter((v) => v.tenant === m[1] && v.cluster_state !== "lost").length })));
on("PUT", /^\/v1\/tenants\/([^/]+)\/quota$/, clusterOnly((m, _q, b) => { QUOTA[m[1]] = Number((b as { max_vms: number }).max_vms); return json({ tenant: m[1], max_vms: QUOTA[m[1]] }); }));
on("GET", /^\/v1\/tokens$/, clusterOnly(() => json({ tokens: TOKENS })));
on("POST", /^\/v1\/tokens$/, clusterOnly((_m, _q, b) => {
  const p = b as { kind?: "admin" | "service"; name?: string; tenants?: string[] }; const id = uuid().slice(0, 12);
  const t: Token = { id, kind: p.kind ?? "service", tenants: p.tenants ?? [], name: p.name ?? "", created_at: now() }; TOKENS.push(t);
  return json({ id, token: `flint_${id}_${uuid().replace(/-/g, "")}`, kind: t.kind, tenants: t.tenants });
}));
on("DELETE", /^\/v1\/tokens\/([^/]+)$/, clusterOnly((m) => { const i = TOKENS.findIndex((t) => t.id === m[1]); if (i >= 0) TOKENS.splice(i, 1); return json({ ok: true }); }));
const storedEvents = (q: URLSearchParams, vmId = q.get("vm_id")) => {
  const since = Number(q.get("since") ?? 0), limit = Number(q.get("limit") ?? 500);
  const out = EVENTS.filter((e) => e.seq > since && (!vmId || e.vm_id === vmId) && (!q.get("tenant") || e.tenant === q.get("tenant")) && (!q.get("type") || e.type === q.get("type"))).slice(0, limit);
  return json({ events: out, next: out.length ? out[out.length - 1].seq : since });
};
on("GET", /^\/v1\/events$/, (_m, q) => storedEvents(q));
on("GET", /^\/vms\/([^/]+)\/timeline$/, (m, q) => storedEvents(q, m[1]));

export async function mockFetch(method: string, url: string, _headers: Record<string, string>, body?: BodyInit): Promise<Response> {
  const u = new URL(url, location.origin);
  for (const [m, re, h] of routes) {
    if (m !== method) continue;
    const match = u.pathname.match(re);
    if (match) {
      await sleep(rnd(15, 45));
      let parsed: unknown;
      if (typeof body === "string") { try { parsed = JSON.parse(body); } catch { parsed = undefined; } }
      return h(match, u.searchParams, parsed, body);
    }
  }
  return err(404, "Not Found");
}

// ---------------- sockets ----------------
export function mockSocket(path: string): Sock {
  const s: Sock = { onopen: null, onmessage: null, onclose: null, send: () => {}, close: () => { cleanup(); s.onclose?.(1000); } };
  let cleanup = () => {};
  const u = new URL(path, location.origin);
  setTimeout(() => {
    s.onopen?.();
    if (u.pathname === "/events") {
      const f = (e: FlintEvent) => s.onmessage?.(JSON.stringify(e));
      subs.add(f); cleanup = () => subs.delete(f);
      return;
    }
    let m = u.pathname.match(/^\/vms\/([^/]+)\/logs$/);
    if (m) {
      const id = m[1]; (LOGS.get(id) ?? []).forEach((l) => s.onmessage?.(l));
      const f = (l: string) => s.onmessage?.(l);
      if (!logSubs.has(id)) logSubs.set(id, new Set());
      logSubs.get(id)!.add(f); cleanup = () => logSubs.get(id)?.delete(f);
      return;
    }
    m = u.pathname.match(/^\/vms\/([^/]+)\/terminal$/);
    if (m) {
      const v = find(m[1]); const name = v?.labels?.name || m[1].slice(0, 8);
      const enc = new TextEncoder(); const out = (t: string) => s.onmessage?.(enc.encode(t).buffer as ArrayBuffer);
      const prompt = `\x1b[38;2;142;181;217mroot@${name}:~#\x1b[0m `;
      out(prompt); let buf = "";
      const R: Record<string, string> = {
        "uname -a": "Linux flint 6.1.190-flint #1 SMP x86_64 GNU/Linux", whoami: "root", ls: "main.py  agent.toml  requirements.txt  out/",
        "env | grep TOKEN": "", "free -m": "               total        used        free\nMem:             488         211         277", pwd: "/root",
      };
      s.send = (d) => {
        const t = typeof d === "string" ? d : new TextDecoder().decode(d instanceof Uint8Array ? d : new Uint8Array(d));
        for (const ch of t) {
          if (ch === "\r") { const c = buf.trim(); buf = ""; out("\r\n"); if (c) out(c in R ? (R[c] ? R[c].replace(/\n/g, "\r\n") + "\r\n" : "") : `sh: ${c.split(" ")[0]}: not found\r\n`); out(prompt); }
          else if (ch === "\x7f") { if (buf) { buf = buf.slice(0, -1); out("\b \b"); } }
          else if (ch >= " ") { buf += ch; out(ch); }
        }
      };
      return;
    }
    s.onclose?.(1011);
  }, 30);
  return s;
}
