import { ApiError } from "../../api/client";
import type { VM } from "../../api/types";
import { Empty } from "../../components/ui";

export const errMsg = (e: unknown): string => (e instanceof ApiError ? e.detail || e.message : e instanceof Error ? e.message : String(e));
export const pretty = (k: string) => { const s = k.replace(/_ms$/, "").replace(/[_.-]+/g, " ").trim(); return s ? s[0].toUpperCase() + s.slice(1) : k; };
export const cmdText = (c: unknown): string => (Array.isArray(c) ? c.join(" ") : c == null || c === "" ? "" : String(c));
export const fmtDuration = (s: number): string => (s >= 3600 && s % 3600 === 0 ? `${s / 3600} h` : s >= 60 && s % 60 === 0 ? `${s / 60} min` : `${s} s`);

export function Head({ title, meta }: { title: React.ReactNode; meta?: React.ReactNode }) {
  return <div className="vm-h"><h3>{title}</h3>{meta != null && <span className="meta">{meta}</span>}</div>;
}

export interface TabProps { vm: VM; running: boolean; onResume: () => void; resuming: boolean }

/** Redacted network policy: domain -> header names. Values are never shown. */
export function networkRules(vm: VM): { domain: string; headers: string[] }[] {
  const allow = vm.networkPolicy?.allow ?? {};
  return Object.entries(allow).map(([domain, rules]) => ({
    domain,
    headers: [...new Set((Array.isArray(rules) ? rules : []).flatMap((r) => (r.transform ?? []).flatMap((t) => Object.keys(t.headers ?? {}))))],
  }));
}

export function NetworkView({ vm }: { vm: VM }) {
  const rules = networkRules(vm);
  if (!rules.length) return <p className="vm-note">No per-domain rules or credential headers are set on this VM.</p>;
  return (
    <table className="tbl vm-tbl" aria-label="Network policy">
      <thead><tr><th scope="col">Domain</th><th scope="col">Headers added by the proxy</th></tr></thead>
      <tbody>
        {rules.map((r) => (
          <tr key={r.domain}>
            <td>{r.domain}</td>
            <td>{r.headers.length ? r.headers.map((h) => <span key={h} className="vm-hdr">{h}</span>) : <span className="dim">no headers</span>}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function NeedsRunning({ vm, what, onResume, resuming }: { vm: VM; what: string; onResume: () => void; resuming: boolean }) {
  return (
    <Empty title={`${vm.name} is ${vm.state === "paused" ? "paused" : vm.state}`}
      action={vm.state === "paused" ? <button className="btn pri" disabled={resuming} onClick={onResume}>Resume</button> : undefined}>
      {what} needs a running VM.
    </Empty>
  );
}
