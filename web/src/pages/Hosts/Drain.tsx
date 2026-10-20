// Drain progress from facts: N = running VMs when the drain started, K = vm.moved events from that host since.
import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { hostActions, errMsg } from "../../api/hosts";
import { useEvents } from "../../api/hooks";
import type { Host, VM } from "../../api/types";
import { useToast } from "../../components/ui";

export interface DrainInfo { host: string; n: number; k: number; state: "draining" | "drained"; left: number; paused: number }
export interface Drains { items: DrainInfo[]; remember: (host: string, n: number) => void }

export function useDrains(hosts: Host[], vms: VM[]): Drains {
  const { events } = useEvents();
  const base = useRef(new Map<string, number>());
  useEffect(() => { for (const id of [...base.current.keys()]) { const h = hosts.find((x) => x.id === id); if (h && h.state !== "draining" && h.state !== "drained") base.current.delete(id); } }, [hosts]);
  const items: DrainInfo[] = [];
  for (const h of hosts) {
    if (h.state !== "draining" && h.state !== "drained") continue;
    const started = events.find((e) => e.type === "host.draining" && e.host_id === h.id)?.ts ?? h.state_since_ms / 1000;
    const k = events.filter((e) => e.type === "vm.moved" && e.from_host === h.id && e.ts >= started).length;
    const on = vms.filter((v) => v.host === h.id);
    const running = on.filter((v) => v.state === "running").length;
    items.push({ host: h.id, state: h.state, k, n: base.current.get(h.id) ?? running + k, left: on.length, paused: on.filter((v) => v.state === "paused").length });
  }
  return { items, remember: (host, n) => { base.current.set(host, n); } };
}

export function DrainLines({ drains, hosts, sel, onPick }: { drains: Drains; hosts: Host[]; sel: string; onPick: (id: string) => void }) {
  const qc = useQueryClient(), toast = useToast();
  if (!drains.items.length) return null;
  const back = async (id: string) => {
    try { await hostActions.uncordon(id); qc.invalidateQueries({ queryKey: ["hosts"] }); toast(`${id} is back in service`); } catch (e) { toast(errMsg(e), { kind: "err" }); }
  };
  return (
    <div className="drains" role="status">
      {drains.items.map((d) => {
        const h = hosts.find((x) => x.id === d.host);
        return d.state === "draining" ? (
          <div className="drain" key={d.host}>
            <span className="chip st-draining"><span className="dot" /></span>
            <span>Moving VMs off <b>{d.host}</b> — moved <b>{Math.min(d.k, Math.max(d.n, d.k))}</b> of <b>{Math.max(d.n, d.k)}</b></span>
            <div className="meter"><i style={{ width: `${d.n ? Math.min(100, (d.k / d.n) * 100) : 0}%`, background: "var(--warn)" }} /></div>
            {d.host !== sel && <button className="btn sm ghost" onClick={() => onPick(d.host)}>Show</button>}
          </div>
        ) : (
          <div className="drain" key={d.host}>
            <span className="chip st-cordoned"><span className="dot" /></span>
            <span><b>{d.host}</b> {d.left === 0 ? "has no VMs left" : `drained · ${d.left} ${d.left === 1 ? "VM stays" : "VMs stay"} (${d.paused} paused) until resumed`}</span>
            <span className="sp-r" />
            {h && <button className="btn sm pri" onClick={() => back(d.host)}>Put back in service</button>}
          </div>
        );
      })}
    </div>
  );
}
