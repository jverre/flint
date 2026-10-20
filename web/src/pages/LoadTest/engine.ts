// Load-test engine: a concurrency-limited create queue, a concurrent cleanup, stats and the
// sessionStorage record of the VMs a run created (so a reload can still clean them up).
import { ApiError } from "../../api/client";
import { createVM, vmActions } from "../../api/hooks";

export interface RunCfg {
  n: number; template: string; templateName: string; conc: number;
  tenant: string; internet: boolean; cluster: boolean;
}

export interface LtResult {
  n: number; name: string; ok: boolean; id: string; host: string;
  clientMs: number; readyMs: number | null; timings: Record<string, number>;
  status: number; detail: string;
  /** ms since the run started, when the response arrived. */
  at: number; wall: number;
}

export interface Hooks {
  onResult: (r: LtResult) => void;
  onIssue: (issued: number) => void;
  onDone: (wallMs: number) => void;
}

export const newRunId = () => Math.random().toString(16).slice(2, 10).padEnd(8, "0");

/** Issue `cfg.n` creates, at most `cfg.conc` in flight. `stop()` stops issuing; in-flight ones finish. */
export function runLoad(cfg: RunCfg, runId: string, hooks: Hooks): { stop: () => void } {
  const t0 = performance.now();
  const w0 = Date.now();
  let next = 0, stopped = false;
  const tag = runId.slice(0, 4);

  const one = async (n: number) => {
    const name = `lt-${tag}-${n + 1}`;
    const s = performance.now();
    let res: Omit<LtResult, "at" | "wall">;
    try {
      const vm = await createVM(
        {
          template: cfg.template,
          tenant: cfg.cluster ? cfg.tenant : undefined,
          name: cfg.cluster ? name : undefined,
          allowInternet: cfg.internet,
          labels: cfg.cluster ? { loadtest: runId } : undefined,
        },
        cfg.cluster,
      );
      remember(runId, vm.id);
      res = {
        n, name, ok: true, id: vm.id, host: cfg.cluster ? vm.host : "local", clientMs: performance.now() - s, readyMs: vm.readyMs,
        timings: vm.timings, status: 200, detail: "",
      };
    } catch (e) {
      const a = e instanceof ApiError ? e : null;
      res = {
        n, name, ok: false, id: "", host: "", clientMs: performance.now() - s, readyMs: null, timings: {},
        status: a?.status ?? 0, detail: a?.detail || (e instanceof Error ? e.message : String(e)),
      };
    }
    hooks.onResult({ ...res, at: performance.now() - t0, wall: Date.now() });
  };

  const worker = async () => {
    while (!stopped && next < cfg.n) {
      const n = next++;
      hooks.onIssue(next);
      await one(n);
    }
  };
  void Promise.all(Array.from({ length: Math.min(cfg.conc, cfg.n) }, worker)).then(() => hooks.onDone(Date.now() - w0));
  return { stop: () => { stopped = true; } };
}

/** Delete VMs, `limit` at a time. 404 counts as already gone. */
export async function killAll(
  ids: string[], onEach: (id: string, err: string | null) => void, limit = 8,
): Promise<{ failed: number; firstError: string }> {
  let i = 0, failed = 0, firstError = "";
  const worker = async () => {
    while (i < ids.length) {
      const id = ids[i++];
      try { await vmActions.kill(id); onEach(id, null); }
      catch (e) {
        if (e instanceof ApiError && e.status === 404) { onEach(id, null); continue; }
        failed++;
        const msg = e instanceof ApiError ? `${e.status} ${e.detail}` : String(e);
        if (!firstError) firstError = msg;
        onEach(id, msg);
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, ids.length) }, worker));
  return { failed, firstError };
}

// ---------- stats (same maths as the TUI benchmark: tui/screens/benchmark.py _compute_stats) ----------
export interface Stats { count: number; min: number; max: number; avg: number; median: number; p5: number; p95: number; p99: number }
export function stats(ts: number[]): Stats | null {
  if (!ts.length) return null;
  const s = [...ts].sort((a, b) => a - b), n = s.length, m = n >> 1;
  const q = (p: number) => s[Math.min(Math.floor(n * p), n - 1)];
  return {
    count: n, min: s[0], max: s[n - 1], avg: s.reduce((a, b) => a + b, 0) / n,
    median: n % 2 ? s[m] : (s[m - 1] + s[m]) / 2, p5: q(0.05), p95: q(0.95), p99: q(0.99),
  };
}

// ---------- heat scale ----------
const HEATS = ["var(--info)", "var(--heat-1)", "var(--heat-2)", "var(--heat-3)", "var(--heat-4)"];
const clamp = (v: number, a = 0, b = 1) => Math.max(a, Math.min(b, v));
export function heat(f: number): string {
  const x = clamp(f) * 4, i = Math.min(3, Math.floor(x)), w = Math.round((x - i) * 100);
  return `color-mix(in srgb, ${HEATS[i + 1]} ${w}%, ${HEATS[i]})`;
}
export interface HeatRange { lo: number; hi: number }
export function heatRange(vals: number[]): HeatRange {
  const s = stats(vals);
  if (!s) return { lo: 0, hi: 100 };
  const lo = vals.length >= 5 ? s.p5 : s.min;
  let hi = vals.length >= 5 ? s.p99 : s.max;
  if (hi - lo < 40) hi = lo + 40;
  return { lo, hi };
}
export const heatAt = (r: HeatRange, v: number) => heat((v - r.lo) / (r.hi - r.lo));

// ---------- session record of created VMs ----------
const KEY = "flint.loadtest";
export interface Saved { runId: string; ids: string[]; at: number }
export function readSaved(): Saved | null {
  try {
    const j = JSON.parse(sessionStorage.getItem(KEY) ?? "null") as Saved | null;
    return j && Array.isArray(j.ids) && j.ids.length ? j : null;
  } catch { return null; }
}
function write(s: Saved | null) {
  if (s && s.ids.length) sessionStorage.setItem(KEY, JSON.stringify(s)); else sessionStorage.removeItem(KEY);
  window.dispatchEvent(new Event("flint:loadtest"));
}
function remember(runId: string, id: string) {
  const s = readSaved();
  write({ runId, ids: [...(s?.ids ?? []), id], at: s?.at ?? Date.now() });
}
export function forget(ids: string[]) {
  const s = readSaved(); if (!s) return;
  const gone = new Set(ids);
  write({ ...s, ids: s.ids.filter((x) => !gone.has(x)) });
}
