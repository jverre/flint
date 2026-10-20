// The selected host: facts, its VMs and the actions that apply to it.
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { effectiveMax, errMsg, hostActions } from "../../api/hosts";
import { ApiError } from "../../api/client";
import type { Host, VM } from "../../api/types";
import { Dialog, Sec, useToast } from "../../components/ui";
import { fmtSince } from "../../kit/format";
import { tenantVar } from "../../kit/svg";
import { HostChip } from "./HostChip";

type Dlg = "drain" | "limit" | "remove" | null;

export function HostPanel({ host: h, vms, rememberDrain }: { host: Host; vms: VM[]; rememberDrain: (id: string, n: number) => void }) {
  const qc = useQueryClient(), toast = useToast();
  const [dlg, setDlg] = useState<Dlg>(null);
  const [busy, setBusy] = useState(false);
  const running = vms.filter((v) => v.state === "running").length;
  const out = h.state === "draining" || h.state === "drained";
  const eff = effectiveMax(h);

  const run = async (fn: () => Promise<unknown>, ok: string) => {
    setBusy(true);
    try { await fn(); qc.invalidateQueries({ queryKey: ["hosts"] }); qc.invalidateQueries({ queryKey: ["vms"] }); toast(ok); return true; }
    catch (e) { toast(errMsg(e), { kind: "err" }); return false; }
    finally { setBusy(false); }
  };

  return (
    <section>
      <Sec l="A" title={h.name || h.id} meta={<HostChip host={h} />} />
      <dl className="kv">
        <dt>Id</dt><dd>{h.id}</dd>
        <dt>Class</dt><dd>{h.host_class || "—"}</dd>
        <dt>Backend</dt><dd>{h.backend || "—"}</dd>
        <dt>Zone</dt><dd>{h.zone || "—"}</dd>
        <dt>Version</dt><dd>{h.version || "—"}</dd>
        <dt>VM limit</dt><dd>{h.active_vms} <span className="dim">/ {eff}{eff !== h.capacity.max_vms ? ` · override, host max ${h.capacity.max_vms}` : ""}</span></dd>
        <dt>Created</dt><dd>{fmtSince(h.created_at_ms)} <span className="dim">ago</span></dd>
        <dt>State since</dt><dd>{fmtSince(h.state_since_ms)} <span className="dim">ago</span></dd>
      </dl>

      <div className="acts-col">
        {out && <button className="btn pri" disabled={busy} onClick={() => run(() => hostActions.uncordon(h.id), `${h.id} is back in service`)}>Put back in service</button>}
        {!out && !h.cordoned && <button className="btn" disabled={busy} onClick={() => run(() => hostActions.cordon(h.id), `${h.id} cordoned: no new placements`)}>Cordon</button>}
        {!out && h.cordoned && <button className="btn" disabled={busy} onClick={() => run(() => hostActions.uncordon(h.id), `${h.id} uncordoned`)}>Uncordon</button>}
        {!out && <button className="btn" disabled={busy} onClick={() => setDlg("drain")}>Move VMs off…</button>}
        <button className="btn" disabled={busy} onClick={() => setDlg("limit")}>Set VM limit…</button>
        <button className="btn danger" disabled={busy} onClick={() => setDlg("remove")}>Remove host…</button>
      </div>
      {!out && h.cordoned && <p className="note">Cordoned: this host takes no new VMs.</p>}

      <div className="lbl" style={{ margin: "12px 0 2px" }}>On this host · {vms.length}</div>
      <div className="hvms">
        {vms.slice(0, 6).map((v) => (
          <Link key={v.id} className="hv" to={`/vms/${v.id}`}>
            <span className={`chip st-${v.state}`} title={v.state}><span className="dot" /></span>
            <b>{v.name}</b>
            <span style={{ color: `var(${tenantVar(v.tenant)})` }}>{v.tenant}</span>
          </Link>
        ))}
        {vms.length > 6 && <div className="note">+ {vms.length - 6} more</div>}
        {!vms.length && <div className="note">No VMs.</div>}
      </div>

      <Dialog open={dlg === "drain"} onClose={() => setDlg(null)} title={`Move VMs off ${h.id}?`} width={480}
        actions={[{ label: "Cancel", kind: "ghost" }, { label: "Move VMs off", kind: "danger", id: "drain-go", onClick: async () => {
          rememberDrain(h.id, running);
          return run(() => hostActions.drain(h.id), `Moving VMs off ${h.id}`);
        } }]}>
        <p className="note" style={{ fontSize: 13 }}>
          {running} {running === 1 ? "VM moves" : "VMs move"} to other hosts, each pauses briefly. Paused VMs stay where they are until resumed.
        </p>
        <p className="note">{h.id} takes no new VMs while it drains.</p>
      </Dialog>
      {dlg === "limit" && <LimitDialog host={h} onClose={() => setDlg(null)} run={run} />}
      {dlg === "remove" && <RemoveDialog host={h} onClose={() => setDlg(null)} run={run} />}
    </section>
  );
}

type Run = (fn: () => Promise<unknown>, ok: string) => Promise<boolean>;

function LimitDialog({ host: h, onClose, run }: { host: Host; onClose: () => void; run: Run }) {
  const [v, setV] = useState(String(h.max_vms_override || ""));
  const n = v.trim() === "" ? 0 : Number(v), valid = Number.isInteger(n) && n >= 0;
  return (
    <Dialog open onClose={onClose} title={`VM limit for ${h.id}`} width={460}
      sub={`The host reports a maximum of ${h.capacity.max_vms}. A limit below it caps placements; 0 removes the limit.`}
      actions={[{ label: "Cancel", kind: "ghost" }, { label: "Save limit", kind: "pri", disabled: !valid, onClick: () => run(() => hostActions.setMaxVms(h.id, n), n ? `${h.id} limited to ${n} VMs` : `${h.id} limit removed`) }]}>
      <label className="fld">Max VMs
        <input autoFocus inputMode="numeric" value={v} placeholder="0" onChange={(e) => setV(e.target.value)} aria-invalid={!valid} />
      </label>
      {!valid && <p className="note errc">Enter a whole number, 0 or more.</p>}
    </Dialog>
  );
}

function RemoveDialog({ host: h, onClose, run }: { host: Host; onClose: () => void; run: Run }) {
  const [typed, setTyped] = useState("");
  const [conflict, setConflict] = useState("");
  const go = async (force: boolean) => {
    const ok = await run(async () => {
      try { await hostActions.remove(h.id, force); }
      catch (e) { if (e instanceof ApiError && e.status === 409) { setConflict(e.detail || "The host still runs VMs."); throw new Error(e.detail || "The host still runs VMs."); } throw e; }
    }, `${h.id} removed`);
    return ok;
  };
  return (
    <Dialog open onClose={onClose} title={`Remove ${h.id}?`} width={480}
      sub="The host is deleted from the cluster."
      actions={[{ label: "Cancel", kind: "ghost" }, { label: conflict ? "Force remove" : "Remove host", kind: "danger", disabled: typed !== h.id, onClick: () => go(!!conflict) }]}>
      {conflict && <p className="note errc" style={{ fontSize: 13 }}>Not removed: {conflict}. Force removes the host while it still has VMs.</p>}
      <label className="fld">Type <b>{h.id}</b> to confirm
        <input autoFocus value={typed} onChange={(e) => setTyped(e.target.value)} spellCheck={false} />
      </label>
    </Dialog>
  );
}

