import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useReducer, useRef, useState } from "react";
import { ApiError } from "../../api/client";
import { invalidateVMs, vmActions } from "../../api/hooks";
import type { VM } from "../../api/types";
import { Dialog, useToast } from "../../components/ui";
import { fmtSince } from "../../kit/format";

const PARALLEL = 6;
const plural = (n: number) => `${n} VM${n === 1 ? "" : "s"}`;
export const errMsg = (e: unknown) => (e instanceof ApiError ? e.detail || `HTTP ${e.status}` : e instanceof Error ? e.message : String(e));

async function pool<T>(items: T[], n: number, fn: (x: T) => Promise<void>) {
  let i = 0;
  await Promise.all(Array.from({ length: Math.min(n, items.length) }, async () => { while (i < items.length) await fn(items[i++]); }));
}

// ---------------- pause / resume ----------------
export function useBulkPauseResume() {
  const qc = useQueryClient(), toast = useToast();
  const [busy, setBusy] = useState(false);
  const run = useCallback(async (kind: "pause" | "resume", targets: VM[], undo = true): Promise<void> => {
    if (!targets.length) return;
    setBusy(true);
    const ok: VM[] = [], fail: string[] = [];
    await pool(targets, PARALLEL, async (v) => {
      try { await vmActions[kind](v.id); ok.push(v); } catch (e) { fail.push(`${v.name}: ${errMsg(e)}`); }
    });
    invalidateVMs(qc);
    setBusy(false);
    const verb = kind === "pause" ? "Paused" : "Resumed";
    const back = kind === "pause" ? "resume" : "pause";
    const action = undo && ok.length ? { action: "Undo", onAction: () => { void run(back, ok, false); } } : {};
    if (fail.length) toast(`${verb} ${ok.length} of ${targets.length}. ${fail.length} failed: ${fail[0]}`, { kind: "err", ...action });
    else toast(`${verb} ${plural(ok.length)}.`, { timeout: 10000, ...action });
  }, [qc, toast]);
  return { busy, run };
}

// ---------------- kill ----------------
type KillSt = { s: "queued" | "run" | "done" | "fail"; ms?: number; detail?: string };

export function useKill() {
  const qc = useQueryClient(), toast = useToast();
  const st = useRef<Record<string, KillSt>>({});
  const [vms, setVms] = useState<VM[] | null>(null);
  const [running, setRunning] = useState(false);
  const [, bump] = useReducer((x: number) => x + 1, 0);
  const set = (id: string, v: KillSt) => { st.current = { ...st.current, [id]: v }; bump(); };

  const runIds = useCallback(async (list: VM[]) => {
    list.forEach((v) => set(v.id, { s: "queued" }));
    setRunning(true);
    await pool(list, PARALLEL, async (v) => {
      set(v.id, { s: "run" });
      const t0 = performance.now();
      try { await vmActions.kill(v.id); set(v.id, { s: "done", ms: Math.round(performance.now() - t0) }); }
      catch (e) { set(v.id, { s: "fail", detail: errMsg(e) }); }
    });
    setRunning(false);
    invalidateVMs(qc);
  }, [qc]);

  const finish = (all: VM[]) => {
    const bad = all.filter((v) => st.current[v.id]?.s === "fail");
    const d = all.length - bad.length;
    if (bad.length) toast(`Killed ${d} of ${all.length}. ${bad.length} failed: ${st.current[bad[0].id].detail}`, { kind: "err" });
    else toast(`Killed ${plural(d)}.`);
  };
  const start = async (list: VM[]) => { st.current = {}; setVms(list); await runIds(list); finish(list); };
  const retry = async (ids: string[]) => { if (!vms) return; await runIds(vms.filter((v) => ids.includes(v.id))); finish(vms); };
  const reset = () => { if (!running) { setVms(null); st.current = {}; } };
  return { vms, st: st.current, running, start, retry, reset };
}

export function KillDialog({ open, onClose, targets, kill }: { open: boolean; onClose: () => void; targets: VM[]; kill: ReturnType<typeof useKill> }) {
  const [typed, setTyped] = useState("");
  const n = targets.length;
  const close = () => { setTyped(""); onClose(); kill.reset(); };

  if (!kill.vms) {
    const ok = typed.trim() === String(n);
    return (
      <Dialog open={open} onClose={close} width={600} title={`Kill ${n} VMs?`} sub="Their running work is lost."
        actions={[
          { label: "Cancel", kind: "ghost" },
          { label: `Kill ${n} VMs`, kind: "danger", disabled: !ok, onClick: () => { void kill.start(targets); return false; } },
        ]}>
        <div className="kill-confirm">
          <table className="tbl" aria-label="VMs to kill">
            <thead><tr><th scope="col">VM</th><th scope="col">Host</th><th scope="col" className="num">Up</th></tr></thead>
            <tbody>{targets.map((v) => (
              <tr key={v.id} style={{ cursor: "default" }}>
                <td><div className="nm"><span className={`chip st-${v.state}`}><span className="dot" /></span><b>{v.name}</b><small>{v.template}</small></div></td>
                <td className="mut">{v.host || "—"}</td><td className="num mut">{fmtSince(v.createdAt)}</td>
              </tr>))}</tbody>
          </table>
          <label className="kq">Type <b>{n}</b> to confirm
            <input type="text" inputMode="numeric" autoFocus autoComplete="off" aria-label={`Type ${n} to confirm`} value={typed} onChange={(e) => setTyped(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && ok) void kill.start(targets); }} />
          </label>
        </div>
      </Dialog>
    );
  }
  const list = kill.vms, S = kill.st;
  const fails = list.filter((v) => S[v.id]?.s === "fail");
  const done = list.filter((v) => S[v.id]?.s === "done").length;
  return (
    <Dialog open={open} onClose={close} width={600}
      title={kill.running ? `Killing ${list.length} VMs` : `Killed ${done} of ${list.length} VMs`}
      sub={kill.running ? `${done} done · up to 6 at a time` : fails.length ? "The VMs that failed are still running. Retry when ready." : "They are gone from the list."}
      actions={[
        ...(!kill.running && fails.length ? [{ label: `Retry ${fails.length} failed`, onClick: () => { void kill.retry(fails.map((v) => v.id)); return false; } }] : []),
        { label: kill.running ? "Hide" : "Close", kind: "ghost" as const },
      ]}>
      <table className="tbl" aria-label="Kill progress">
        <thead><tr><th scope="col">VM</th><th scope="col">Progress</th></tr></thead>
        <tbody>{list.map((v) => {
          const r = S[v.id] ?? { s: "queued" as const };
          return (
            <tr key={v.id} className={r.s === "fail" ? "kfail" : r.s === "done" ? "kdone" : ""} style={{ cursor: "default" }}>
              <td><div className="nm"><b>{v.name}</b></div></td>
              <td>
                {r.s === "queued" && <span className="dim">· queued</span>}
                {r.s === "run" && <span className="row"><span className="chip st-running"><span className="dot" /></span>killing…</span>}
                {r.s === "done" && <span><span className="okc">✓</span> killed <span className="dim">{r.ms} ms</span></span>}
                {r.s === "fail" && <span className="errc">✕ {r.detail} <button className="btn sm" disabled={kill.running} onClick={() => void kill.retry([v.id])}>Retry</button></span>}
              </td>
            </tr>);
        })}</tbody>
      </table>
    </Dialog>
  );
}

export function SelectionBar({ sel, busy, onPause, onResume, onKill, onClear }: {
  sel: VM[]; busy: boolean; onPause: () => void; onResume: () => void; onKill: () => void; onClear: () => void;
}) {
  const r = sel.filter((v) => v.state === "running").length, p = sel.filter((v) => v.state === "paused").length;
  return (
    <div className="vms-selbar" role="toolbar" aria-label="Selection">
      <span className="cnt">{sel.length} selected</span>
      <span className="f">{r ? `${r} running` : ""}{r && p ? " · " : ""}{p ? `${p} paused` : ""}</span>
      {r > 0 && <button className="btn" disabled={busy} onClick={onPause}>Pause</button>}
      {p > 0 && <button className="btn" disabled={busy} onClick={onResume}>Resume</button>}
      <button className="btn danger" disabled={busy} onClick={onKill}>Kill</button>
      <span className="sp-r" />
      <button className="btn ghost" onClick={onClear}>Clear</button>
    </div>
  );
}
