import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError } from "../api/client";
import { invalidateVMs, noteRecent, useHosts, useIsAdmin, useIsCluster, useVm, vmActions } from "../api/hooks";
import type { VM } from "../api/types";
import { useKeyHints } from "../components/Shell";
import { Dialog, Empty, StateChip, Tabs, useTick, useToast } from "../components/ui";
import { fmtClock, fmtMs, fmtSince } from "../kit/format";
import { usePageKeys } from "../kit/keys";
import { errMsg, fmtDuration } from "./Vm/common";
import Files from "./Vm/Files";
import Logs from "./Vm/Logs";
import Overview from "./Vm/Overview";
import Settings from "./Vm/Settings";
import ShellTab from "./Vm/Shell";
import "./vm.css";

const TABS = ["overview", "shell", "logs", "files", "settings"] as const;
type Tab = (typeof TABS)[number];
const LABEL: Record<Tab, string> = { overview: "Overview", shell: "Shell", logs: "Logs", files: "Files", settings: "Settings" };

export default function VmPage() {
  const { id = "", tab: tabParam } = useParams();
  const vmq = useVm(id);
  const tab: Tab = (TABS as readonly string[]).includes(tabParam ?? "") ? (tabParam as Tab) : "overview";
  useEffect(() => { if (id) noteRecent(id); }, [id]);

  if (vmq.isLoading) return <div className="page"><div className="vm-loading"><span className="sk" style={{ width: 220, height: 22 }} /><span className="sk" style={{ width: "60%" }} /><span className="sk" style={{ width: "100%", height: 70 }} /></div></div>;
  if (vmq.error instanceof ApiError && vmq.error.status === 404)
    return <div className="page"><Empty title="VM not found" action={<Link className="btn" to="/vms">Back to VMs</Link>}>No VM with id <code>{id}</code>. It may have been killed.</Empty></div>;
  if (vmq.isError || !vmq.data)
    return <div className="page"><Empty title="Could not load this VM" action={<button className="btn" onClick={() => vmq.refetch()}>Retry</button>}>{errMsg(vmq.error)}</Empty></div>;
  return <Detail vm={vmq.data} tab={tab} />;
}

function Detail({ vm, tab }: { vm: VM; tab: Tab }) {
  const nav = useNavigate(), qc = useQueryClient(), toast = useToast();
  const cluster = useIsCluster(), admin = useIsAdmin();
  const [busy, setBusy] = useState("");
  const [killing, setKilling] = useState(false), [moving, setMoving] = useState(false), [dest, setDest] = useState("");
  const [seen, setSeen] = useState<Set<Tab>>(new Set([tab]));
  const running = vm.state === "running", paused = vm.state === "paused";
  useTick(30_000);
  useEffect(() => { setSeen((s) => (s.has(tab) ? s : new Set(s).add(tab))); }, [tab]);

  const go = (t: string) => nav(t === "overview" ? `/vms/${vm.id}` : `/vms/${vm.id}/${t}`);
  useKeyHints([{ k: "1–5", label: "Switch tab" }]);
  usePageKeys(Object.fromEntries(TABS.map((t, i) => [String(i + 1), () => go(t)])));

  const act = async (what: string, fn: () => Promise<unknown>, done: string) => {
    setBusy(what);
    try { await fn(); toast(done); invalidateVMs(qc, vm.id); return true; }
    catch (e) { toast(errMsg(e), { kind: "err" }); return false; }
    finally { setBusy(""); }
  };
  const resume = () => act("resume", () => vmActions.resume(vm.id), `Resumed ${vm.name}.`);
  const hosts = useHosts(cluster && admin && moving);
  const targets = (hosts.data ?? []).filter((h) => h.id !== vm.host && h.state === "ready" && !h.cordoned);

  const facts = [
    vm.template,
    cluster && vm.host && vm.host,
    vm.backend,
    vm.createdAt && `up ${fmtSince(vm.createdAt)}`,
    vm.readyMs != null && `started in ${fmtMs(vm.readyMs)}`,
    cluster && vm.availability,
    vm.timeoutSeconds && `auto-${vm.timeoutPolicy ?? "kill"} after ${fmtDuration(vm.timeoutSeconds)}`,
  ].filter(Boolean);
  const props = { vm, running, onResume: resume, resuming: busy === "resume" };

  return (
    <div className="page vm-page">
      <header className="page-head vm-head">
        <div className="grow">
          <div className="vm-ttl">
            <Link to="/vms" className="vm-back" aria-label="Back to VMs" title="Back to VMs">←</Link>
            <h1>{vm.name}</h1><StateChip state={vm.state} />
            {vm.tenant && <span className="vm-tenant">{vm.tenant}</span>}
          </div>
          <div className="facts">{facts.join(" · ")}</div>
          {vm.error && <div className="facts errc" role="alert">{vm.error}</div>}
        </div>
        <div className="acts">
          {running && <button className="btn pri" onClick={() => go("shell")}>Connect</button>}
          {running && <button className="btn" disabled={!!busy} onClick={() => act("pause", () => vmActions.pause(vm.id), `Paused ${vm.name}.`)}>Pause</button>}
          {paused && <button className="btn pri" disabled={!!busy} onClick={resume}>Resume</button>}
          {cluster && running && (
            <>
              <button className="btn" disabled={!!busy} onClick={() => act("ckpt", () => vmActions.checkpoint(vm.id), `Checkpoint saved for ${vm.name}.`)}>Save checkpoint</button>
              {vm.checkpointAt && <span className="dim vm-ck" title={new Date(vm.checkpointAt * 1000).toLocaleString()}>saved {fmtClock(vm.checkpointAt).slice(0, 5)}</span>}
            </>
          )}
          {cluster && admin && (running || paused) && <button className="btn" disabled={!!busy} onClick={() => { setDest(""); setMoving(true); }}>Move to another host</button>}
          <button className="btn danger" disabled={!!busy} onClick={() => setKilling(true)}>Kill</button>
        </div>
      </header>

      <div className="vm-tbar"><Tabs tabs={TABS.map((t) => ({ id: t, label: LABEL[t] }))} active={tab} onChange={go} /></div>

      <div className="vm-pane" role="tabpanel" aria-label={LABEL[tab]}>
        {tab === "overview" && <Overview {...props} />}
        {seen.has("shell") && <div className="vm-fill" hidden={tab !== "shell"}><ShellTab {...props} active={tab === "shell"} /></div>}
        {tab === "logs" && <Logs {...props} />}
        {tab === "files" && <Files {...props} />}
        {tab === "settings" && <Settings {...props} />}
      </div>

      <Dialog open={killing} onClose={() => setKilling(false)} title={`Kill ${vm.name}?`} sub="The VM stops and its workspace is deleted. This can't be undone." width={460}
        actions={[{ label: "Cancel", kind: "ghost" }, { label: "Kill VM", kind: "danger", onClick: async () => {
          const ok = await act("kill", () => vmActions.kill(vm.id), `Killed ${vm.name}.`);
          if (ok) { qc.removeQueries({ queryKey: ["vm", vm.id] }); nav("/vms"); }
          return ok;
        } }]}>
        <p className="vm-note">{cluster ? "To keep its state, save a checkpoint first." : "Anything not copied out of the VM is lost."}</p>
      </Dialog>

      <Dialog open={moving} onClose={() => setMoving(false)} title={`Move ${vm.name} to another host`} sub={`Currently on ${vm.host || "unknown host"}. The VM is moved live; its shells disconnect.`} width={480}
        actions={[{ label: "Cancel", kind: "ghost" }, { label: "Move", kind: "pri", disabled: hosts.isLoading, onClick: async () => {
          return act("move", () => vmActions.migrate(vm.id, dest || undefined), `Moved ${vm.name}${dest ? ` to ${dest}` : ""}.`);
        } }]}>
        {hosts.isError ? <p className="vm-note errc">{errMsg(hosts.error)}</p> : (
          <label className="vm-lbl">Destination host
            <select className="vm-input" value={dest} onChange={(e) => setDest(e.target.value)} disabled={hosts.isLoading}>
              <option value="">Let the scheduler choose</option>
              {targets.map((h) => <option key={h.id} value={h.id}>{h.name} · {h.zone} · {h.active_vms} VMs</option>)}
            </select>
          </label>
        )}
      </Dialog>
    </div>
  );
}
