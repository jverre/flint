import { useSearchParams } from "react-router-dom";
import { effectiveMax } from "../api/hosts";
import { useHosts, useIsAdmin, usePlatform, useVms } from "../api/hooks";
import type { Host } from "../api/types";
import { useThemeVersion } from "../components/Shell";
import { Empty, Kpis, Sec, SkeletonRows, useTick } from "../components/ui";
import { fmtSince } from "../kit/format";
import Floor from "./Hosts/Floor";
import { HostChip } from "./Hosts/HostChip";
import { HostFeed } from "./Hosts/HostFeed";
import { HostPanel } from "./Hosts/HostPanel";
import { useDrains, DrainLines } from "./Hosts/Drain";
import "./hosts.css";

const GIB = (mb: number) => (mb / 1024).toFixed(mb % 1024 ? 1 : 0);
const age = (h: Host) => Math.max(0, (Date.now() - h.last_heartbeat_ms) / 1000);
const fmtAge = (s: number) => (s < 10 ? s.toFixed(1) : s < 120 ? String(Math.round(s)) : fmtSince(Date.now() - s * 1000));

export default function HostsPage() {
  const plat = usePlatform().data, admin = useIsAdmin();
  if (!plat) return <div className="page"><h1>Hosts</h1></div>;
  if (plat.mode !== "cluster") return <div className="page"><h1>Hosts</h1><Empty title="Hosts only exist in a Flint cluster">This is a single daemon. Start a control plane and join hosts to see them here.</Empty></div>;
  if (!admin) return <div className="page"><h1>Hosts</h1><Empty title="Hosts need an admin token">This token is a service token. Sign in with an admin token to manage hosts.</Empty></div>;
  return <HostsView />;
}

function HostsView() {
  const hosts = useHosts(), vms = useVms();
  const [params, setParams] = useSearchParams();
  const themeV = useThemeVersion();
  const drains = useDrains(hosts.data ?? [], vms.data ?? []);
  useTick(1000);

  const list = hosts.data ?? [], all = vms.data ?? [];
  const selId = params.get("host") && list.some((h) => h.id === params.get("host")) ? params.get("host")! : list[0]?.id ?? "";
  const sel = list.find((h) => h.id === selId);
  const pick = (id: string) => setParams((p) => { p.set("host", id); return p; }, { replace: true });

  if (hosts.isError) return <div className="page"><h1>Hosts</h1><Empty title="Couldn't load hosts" action={<button className="btn" onClick={() => hosts.refetch()}>Retry</button>}>{(hosts.error as Error).message}</Empty></div>;

  const ready = list.filter((h) => h.state === "ready").length, cordoned = list.filter((h) => h.cordoned).length;
  const placed = list.reduce((a, h) => a + h.active_vms, 0), cap = list.reduce((a, h) => a + effectiveMax(h), 0);
  const memTot = list.reduce((a, h) => a + h.capacity.mem_total_mb, 0), memUsed = memTot - list.reduce((a, h) => a + h.capacity.mem_available_mb, 0);
  const oldest = list.length ? Math.max(...list.map(age)) : 0;
  const zones = new Set(list.map((h) => h.zone).filter(Boolean)), versions = [...new Set(list.map((h) => h.version).filter(Boolean))].sort();

  return (
    <div className="page hosts">
      <header className="page-head">
        <div>
          <h1>Hosts</h1>
          <div className="facts">
            <b>{ready} of {list.length} ready</b>
            {cordoned > 0 && <> · {cordoned} cordoned</>}
            {zones.size > 0 && <> · {zones.size} {zones.size === 1 ? "zone" : "zones"}</>}
            {versions.length > 0 && <> · {versions.length > 1 ? "versions " : "version "}{versions.join(", ")}</>}
          </div>
        </div>
        <Kpis items={[
          { label: "Ready", value: ready, unit: `/ ${list.length}`, col: "var(--ok)" },
          { label: "VMs placed", value: placed, unit: `/ ${cap}`, col: "var(--spark)" },
          { label: "Memory used", value: GIB(memUsed), unit: `/ ${GIB(memTot)} GiB` },
          { label: "Oldest heartbeat", value: fmtAge(oldest), unit: "s" },
        ]} />
      </header>

      <div className="page-body">
        <div className="main-col" style={{ gap: 10 }}>
          <div className="scroll tblwrap">
            <table className="tbl" aria-label="Hosts">
              <thead><tr><th scope="col">Host</th><th scope="col">Version</th><th scope="col">State</th><th scope="col">VMs</th><th scope="col">Memory · GiB</th><th scope="col" className="num">Heartbeat</th></tr></thead>
              <tbody>
                {hosts.isLoading && <SkeletonRows rows={4} cols={6} />}
                {list.map((h) => {
                  const eff = effectiveMax(h), used = h.capacity.mem_total_mb - h.capacity.mem_available_mb;
                  return (
                    <tr key={h.id} className={h.id === selId ? "sel" : ""} onClick={() => pick(h.id)}>
                      <td><div className="nm"><button className="nmbtn" onClick={() => pick(h.id)}>{h.name || h.id}</button><small>{h.zone}</small></div></td>
                      <td className="dim">{h.version}</td>
                      <td><HostChip host={h} /></td>
                      <td><div className="mc"><div className="meter"><i style={{ width: `${eff ? Math.min(100, (h.active_vms / eff) * 100) : 0}%`, background: "var(--slate)" }} /></div>{h.active_vms}<span className="dim">/ {eff}</span></div></td>
                      <td><div className="mc"><div className="meter"><i style={{ width: `${h.capacity.mem_total_mb ? Math.max(1.5, (used / h.capacity.mem_total_mb) * 100) : 0}%` }} /></div>{GIB(used)}<span className="dim">/ {GIB(h.capacity.mem_total_mb)}</span></div></td>
                      <td className="num">{fmtAge(age(h))} s</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {!hosts.isLoading && !list.length && <Empty title="No hosts have joined">Create a join token with <code>POST /v1/join-tokens</code> and start flint-agent on a machine.</Empty>}
          </div>

          <DrainLines drains={drains} hosts={list} sel={selId} onPick={pick} />

          <div className="mapbox">
            {list.length > 0 && <Floor hosts={list} vms={all} sel={selId} onHost={pick} themeV={themeV} />}
            <span className="h">Each slab is a host, each cube a VM · click a cube to open it</span>
            <span className="lg"><span>outline = tenant</span><span><span className="hz" /> cordoned, draining or drained</span></span>
          </div>
        </div>

        <aside className="side" aria-label="Selected host and events">
          {sel && <HostPanel key={sel.id} host={sel} vms={all.filter((v) => v.host === sel.id)} rememberDrain={drains.remember} />}
          <section className="grow-sec">
            <Sec l="B" title="Host events" />
            <div className="feedbox"><HostFeed vms={all} /></div>
          </section>
        </aside>
      </div>
    </div>
  );
}
