import type { ReactNode } from "react";
import { useMemo, useRef } from "react";
import { useNavigate } from "react-router-dom";
import type { FlintEvent, Health, Host, VM } from "../../api/types";
import { Feed, Sec, type FeedItem } from "../../components/ui";
import { useThemeVersion } from "../../components/Shell";
import { fmtClock, fmtMs, stateLabel } from "../../kit/format";
import { isoMarkup, isoProjection, type IsoVM } from "../../kit/svg";

// ---------------- placement floor ----------------
const W = 300, H = 230;

function build(hosts: Host[], vms: VM[], sel: string | null) {
  const n = hosts.length, cols = n <= 4 ? n : 4;
  const layout: [number, number][] = hosts.map((_, i) => [(i % cols) * 7.5, Math.floor(i / cols) * 6.5]);
  // bounding box of the floor in unit projection, then fit it to the svg
  const P0 = isoProjection({ cell: 1, origin: [0, 0] });
  const pts: [number, number][] = [];
  layout.forEach(([bx, by]) => [[0, 0, 0], [6, 0, 0], [0, 5, 0], [6, 5, 0], [0, 5, -0.45], [6, 5, -0.45], [0, 0, 1.5], [6, 0, 1.5], [6, 5, 1.5]]
    .forEach(([dx, dy, z]) => pts.push(P0(bx + dx, by + dy, z))));
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
  const padL = 58, padR = 8, padT = 8, padB = 22;
  const cell = Math.max(3, Math.min(14, (W - padL - padR) / (maxX - minX), (H - padT - padB) / (maxY - minY)));
  const ox = padL + ((W - padL - padR) - (maxX - minX) * cell) / 2 - minX * cell;
  const oy = padT + ((H - padT - padB) - (maxY - minY) * cell) / 2 - minY * cell;
  const iso: IsoVM[] = vms.map((v) => ({ id: v.id, name: v.name, host: v.host, tenant: v.tenant, state: v.state }));
  const { html } = isoMarkup(
    hosts.map((h) => ({ id: h.id, label: h.name, state: h.cordoned && h.state === "ready" ? "cordoned" : h.state })),
    iso, { cell, origin: [ox, oy], layout, sel });
  return html.replace(/<text[^>]*>‖<\/text>/g, "");
}

export function Placement({ hosts, vms, sel }: { hosts: Host[] | undefined; vms: VM[]; sel: string | null }) {
  const nav = useNavigate();
  const tv = useThemeVersion();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const html = useMemo(() => (hosts?.length ? build(hosts, vms, sel) : ""), [hosts, vms, sel, tv]);
  return (
    <section>
      <Sec l="A" title="Placement" meta={hosts ? `${hosts.length} hosts` : undefined} />
      <button className="vms-map" onClick={() => nav("/hosts")} aria-label="Open hosts">
        {hosts?.length
          ? <svg width={W} height={H} aria-hidden="true" dangerouslySetInnerHTML={{ __html: html }} />
          : <span className="dim vms-map-empty">{hosts ? "No hosts registered" : "Loading hosts…"}</span>}
        <span className="h">open hosts ↗</span>
      </button>
    </section>
  );
}

// ---------------- activity ----------------
const OK = "var(--ok)", DIM = "var(--dim)", ERR = "var(--err)", SPARK = "var(--spark)", BLUE = "var(--blue)", WARN = "var(--warn)";

function describe(e: FlintEvent, vms: Map<string, VM>, names: Map<string, string>): Pick<FeedItem, "color" | "body"> | null {
  if (!e.type.startsWith("vm.")) return null;
  const known = e.vm_id ? vms.get(e.vm_id) : undefined;
  const lbl = e.vm?.labels?.name || known?.name || (e.vm_id && names.get(e.vm_id)) || (e.vm_id ?? "").slice(0, 8) || "VM";
  const n = <b>{lbl}</b>;
  const host = String(e.host_id ?? e.vm?.host_id ?? known?.host ?? "");
  switch (e.type) {
    case "vm.created": {
      const ms = e.vm?.ready_time_ms;
      return { color: OK, body: <>{n} started{host && <> on {host}</>}{ms != null && <> · {fmtMs(ms)}</>}</> };
    }
    case "vm.paused": return { color: DIM, body: <>{n} paused</> };
    case "vm.resumed": return { color: SPARK, body: <>{n} resumed</> };
    case "vm.deleted": return { color: DIM, body: <>{n} deleted</> };
    case "vm.moved": return { color: BLUE, body: <>{n} moved{e.from_host != null && <> from {String(e.from_host)}</>}{e.to_host != null && <> to {String(e.to_host)}</>}</> };
    case "vm.checkpointed": return { color: DIM, body: <>{n} saved a checkpoint</> };
    case "vm.state_changed": {
      const to = String(e.to ?? "");
      return { color: to === "error" || to === "lost" ? ERR : to === "running" ? OK : WARN, body: <>{n} is now {stateLabel(to).toLowerCase()}{e.from ? <> <span className="dim">(was {stateLabel(String(e.from)).toLowerCase()})</span></> : null}</> };
    }
    default: return { color: DIM, body: <>{n} {e.type.slice(3).replace(/_/g, " ")}</> };
  }
}

export function Activity({ letter, events, vms }: { letter: string; events: (FlintEvent & { _k: string })[]; vms: VM[] }) {
  // names of VMs seen so far, so "deleted" events can still say who it was
  const names = useRef(new Map<string, string>());
  vms.forEach((v) => { if (v.hasName) names.current.set(v.id, v.name); });
  const items = useMemo(() => {
    const byId = new Map(vms.map((v) => [v.id, v]));
    const out: FeedItem[] = [];
    for (const e of events) {
      const d = describe(e, byId, names.current);
      if (!d) continue;
      const ms = e.ts > 1e12 ? e.ts : e.ts * 1000;
      out.push({ key: e._k, time: fmtClock(e.ts), fresh: Date.now() - ms < 4000, ...d });
      if (out.length >= 30) break;
    }
    return out;
  }, [events, vms]);
  return (
    <section className="vms-act">
      <Sec l={letter} title="Activity" meta={<span className="chip st-running"><span className="dot" />live</span>} />
      {items.length ? <Feed items={items} /> : <p className="dim vms-quiet">No VM events yet. Starts, pauses and deletes show up here as they happen.</p>}
    </section>
  );
}

// ---------------- daemon: this host ----------------
export function ThisHost({ health }: { health: Health | undefined }) {
  const row = (k: string, v: ReactNode) => <div className="kv"><span className="mut">{k}</span><span>{v}</span></div>;
  return (
    <section>
      <Sec l="B" title="This host" />
      {row("Backend", health?.backend_kind ?? "—")}
      {row("Golden snapshot", health ? (health.golden_snapshot_ready ? <span className="okc">ready</span> : <span className="wrn">not ready</span>) : "—")}
      {row("Storage", health?.storage_backend ? <>{health.storage_backend} {health.storage_healthy === false ? <span className="errc">unhealthy</span> : <span className="okc">healthy</span>}</> : "—")}
    </section>
  );
}
