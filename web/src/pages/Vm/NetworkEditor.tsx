// Edit a VM's network policy: per domain, the headers the proxy adds.
// Header values are write-only: existing ones are never shown, and an empty
// field keeps the current value, but only while its domain and name are unchanged.
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { invalidateVMs, setNetworkPolicy, useNetworkPolicy } from "../../api/hooks";
import type { NetworkPolicy, VM } from "../../api/types";
import { useToast } from "../../components/ui";
import { errMsg } from "./common";

interface Hdr { key: number; name: string; value: string; orig?: string } // orig: the name as loaded
interface Row { key: number; domain: string; headers: Hdr[]; orig?: string } // orig: the domain as loaded
type Values = Record<string, Record<string, string>>; // loaded domain -> header -> value

let seq = 0;
const HEADER = /^[A-Za-z0-9!#$%&'*+.^_`|~-]+$/;

function load(p: NetworkPolicy | null): { rows: Row[]; values: Values } {
  const values: Values = {};
  const rows = Object.entries(p?.allow ?? {}).map(([domain, rules]) => {
    const hs: Record<string, string> = {};
    for (const r of Array.isArray(rules) ? rules : []) for (const t of r.transform ?? []) Object.assign(hs, t.headers ?? {});
    values[domain] = hs;
    return { key: ++seq, domain, orig: domain, headers: Object.keys(hs).map((name) => ({ key: ++seq, name, value: "", orig: name })) };
  });
  return { rows, values };
}

/** The current value a header keeps when its field is left empty. */
const kept = (values: Values, r: Row, h: Hdr): string | undefined =>
  r.orig !== undefined && r.domain.trim() === r.orig && h.orig !== undefined && h.name.trim() === h.orig ? values[r.orig]?.[h.orig] : undefined;

function problem(rows: Row[], values: Values): string {
  const domains = new Set<string>();
  for (const r of rows) {
    const d = r.domain.trim();
    if (!d) return "Every rule needs a domain.";
    if (domains.has(d.toLowerCase())) return `${d} is listed twice.`;
    domains.add(d.toLowerCase());
    const names = new Set<string>();
    for (const h of r.headers) {
      const n = h.name.trim();
      if (!HEADER.test(n)) return `${d}: "${n || "(empty)"}" is not a valid header name.`;
      if (names.has(n.toLowerCase())) return `${d}: ${n} is set twice.`;
      names.add(n.toLowerCase());
      if (!h.value && kept(values, r, h) === undefined) return `${d}: ${n} needs a value.`;
    }
  }
  return "";
}

function toPolicy(rows: Row[], values: Values): NetworkPolicy {
  const allow: NonNullable<NetworkPolicy["allow"]> = {};
  for (const r of rows) {
    const headers: Record<string, string> = {};
    for (const h of r.headers) headers[h.name.trim()] = h.value || kept(values, r, h) || "";
    allow[r.domain.trim()] = Object.keys(headers).length ? [{ transform: [{ headers }] }] : [];
  }
  return { allow };
}

export default function NetworkEditor({ vm, onDone }: { vm: VM; onDone: () => void }) {
  const policy = useNetworkPolicy(vm.id);
  if (policy.isError) return <p className="vm-strip-err" role="alert">{errMsg(policy.error)}</p>;
  if (!policy.isSuccess) return <p className="dim">Loading…</p>;
  return <PolicyForm vm={vm} initial={policy.data} onDone={onDone} />;
}

function PolicyForm({ vm, initial, onDone }: { vm: VM; initial: NetworkPolicy | null; onDone: () => void }) {
  const toast = useToast(), qc = useQueryClient();
  // Edits start from the policy as loaded; a later refetch doesn't discard them.
  const [state, setState] = useState(() => load(initial));
  const [busy, setBusy] = useState(false);

  const { rows, values } = state;
  const set = (next: Row[]) => setState({ rows: next, values });
  const edit = (k: number, f: (r: Row) => Row) => set(rows.map((r) => (r.key === k ? f(r) : r)));
  const editH = (rk: number, hk: number, p: Partial<Hdr>) => edit(rk, (r) => ({ ...r, headers: r.headers.map((h) => (h.key === hk ? { ...h, ...p } : h)) }));
  const err = problem(rows, values);
  const save = async () => {
    setBusy(true);
    try {
      await setNetworkPolicy(vm.id, toPolicy(rows, values));
      toast("Network policy saved.");
      invalidateVMs(qc, vm.id);
      onDone();
    } catch (e) { toast(errMsg(e), { kind: "err" }); }
    finally { setBusy(false); }
  };

  return (
    <form className="vm-np" onSubmit={(e) => { e.preventDefault(); if (!err) save(); }}>
      {rows.map((r) => (
        <fieldset key={r.key} className="vm-np-row">
          <div className="vm-np-dom">
            <input className="vm-input" aria-label="Domain" placeholder="api.example.com" value={r.domain} onChange={(e) => edit(r.key, (x) => ({ ...x, domain: e.target.value }))} />
            <button type="button" className="btn sm ghost" onClick={() => set(rows.filter((x) => x.key !== r.key))}>Remove</button>
          </div>
          {r.headers.map((h) => (
            <div key={h.key} className="vm-np-hdr">
              <input className="vm-input" aria-label="Header name" placeholder="Authorization" value={h.name} onChange={(e) => editH(r.key, h.key, { name: e.target.value })} />
              <input className="vm-input" aria-label={`${h.name || "Header"} value`} type="password" autoComplete="off"
                placeholder={kept(values, r, h) !== undefined ? "unchanged" : "value"} value={h.value} onChange={(e) => editH(r.key, h.key, { value: e.target.value })} />
              <button type="button" className="ib" aria-label={`Remove ${h.name || "header"}`} onClick={() => edit(r.key, (x) => ({ ...x, headers: x.headers.filter((y) => y.key !== h.key) }))}>×</button>
            </div>
          ))}
          <button type="button" className="btn sm" onClick={() => edit(r.key, (x) => ({ ...x, headers: [...x.headers, { key: ++seq, name: "", value: "" }] }))}>Add header</button>
        </fieldset>
      ))}
      <div className="vm-np-acts">
        <button type="button" className="btn sm" onClick={() => set([...rows, { key: ++seq, domain: "", headers: [{ key: ++seq, name: "", value: "" }] }])}>Add domain</button>
        {err && <span className="vm-np-err" role="alert">{err}</span>}
        <span className="vm-np-gap" />
        <button type="button" className="btn ghost" onClick={onDone}>Cancel</button>
        <button className="btn pri" disabled={!!err || busy}>{busy ? "Saving…" : "Save"}</button>
      </div>
    </form>
  );
}
