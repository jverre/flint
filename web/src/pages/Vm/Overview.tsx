import { useMemo } from "react";
import { useEvents, useIsCluster, useMetrics, useProcesses } from "../../api/hooks";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { FlintEvent, VM } from "../../api/types";
import { Feed, Sec, type FeedItem } from "../../components/ui";
import { fmtClock, fmtMs } from "../../kit/format";
import { cmdText, errMsg, Head, NetworkView, pretty, type TabProps } from "./common";
import { useToast } from "../../components/ui";

const MIB = 1048576;

function Area({ data, max, col, label }: { data: number[]; max: number; col: string; label: string }) {
  const W = 300, H = 44;
  const pts = data.map((v, i) => `${((i / Math.max(1, data.length - 1)) * W).toFixed(1)},${(H - 1 - (Math.min(v, max) / max) * (H - 4)).toFixed(1)}`).join(" ");
  return (
    <svg className="vm-area" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label={label}>
      {data.length > 1 && <>
        <polygon points={`0,${H} ${pts} ${W},${H}`} style={{ fill: col, fillOpacity: 0.14 }} />
        <polyline points={pts} vectorEffect="non-scaling-stroke" style={{ fill: "none", stroke: col, strokeWidth: 1.4 }} />
      </>}
    </svg>
  );
}

function MetricsStrip({ vm, running }: { vm: VM; running: boolean }) {
  const m = useMetrics(vm.id, running);
  const s = m.data ?? [];
  const cpu = s.map((x) => x.cpu_percent), rss = s.map((x) => x.rss_bytes / MIB);
  const last = (a: number[]) => (a.length ? a[a.length - 1] : null);
  const cpuMax = Math.max(100, ...cpu), rssMax = Math.max(64, ...rss) * 1.15;
  if (!running) return <div className="vm-strip"><div className="vm-cell vm-nometrics">Paused: no live metrics</div></div>;
  return (
    <div className="vm-strip">
      <div className="vm-cell">
        <div className="vm-t"><span>CPU</span><b>{last(cpu) == null ? "—" : `${Math.round(last(cpu)!)} %`}</b><span className="r">last 60 s</span></div>
        <Area data={cpu} max={cpuMax} col="var(--data-neutral)" label="CPU over the last 60 seconds" />
      </div>
      <div className="vm-cell">
        <div className="vm-t"><span>Memory (RSS)</span><b>{last(rss) == null ? "—" : `${Math.round(last(rss)!)} MiB`}</b><span className="r">last 60 s</span></div>
        <Area data={rss} max={rssMax} col="var(--heat-3)" label="Resident memory over the last 60 seconds" />
      </div>
      {m.isError && <div className="vm-strip-err">Metrics unavailable: {errMsg(m.error)}</div>}
    </div>
  );
}

function Processes({ vm, running }: { vm: VM; running: boolean }) {
  const p = useProcesses(vm.id, running);
  const list = p.data ?? [];
  return (
    <section>
      <Head title="Processes" meta={running && p.data ? `${list.length}` : undefined} />
      {!running ? <p className="vm-note">Nothing runs while the VM is {vm.state}.</p>
        : p.isLoading ? <p className="vm-note">Loading…</p>
        : p.isError ? <p className="vm-note errc">Could not list processes: {errMsg(p.error)} <button className="btn sm" onClick={() => p.refetch()}>Retry</button></p>
        : !list.length ? <p className="vm-note">No processes reported.</p>
        : (
          <table className="tbl vm-tbl" aria-label="Processes">
            <thead><tr><th scope="col" className="num">PID</th><th scope="col">Command</th><th scope="col">Tag</th><th scope="col">Running</th></tr></thead>
            <tbody>
              {list.map((x, i) => (
                <tr key={`${x.pid}-${i}`}>
                  <td className="num">{x.pid ?? "—"}</td>
                  <td className="vm-cmd" title={cmdText(x.cmd)}>{cmdText(x.cmd) || <span className="dim">—</span>}{x.pty ? <span className="dim"> · pty</span> : null}</td>
                  <td>{x.tag ? <span className="tag dim">{x.tag}</span> : <span className="dim">—</span>}</td>
                  <td>{x.running === undefined ? <span className="dim">—</span> : x.running ? "yes" : <span className="dim">exited{x.exit_code != null ? ` (${x.exit_code})` : ""}</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
    </section>
  );
}

const PHASE_COLS = ["--fg-label", "--info", "--heat-2", "--heat-3", "--ok", "--tenant-3", "--heat-4", "--tenant-2"];
function Startup({ vm }: { vm: VM }) {
  const ph = Object.entries(vm.timings).filter(([, v]) => typeof v === "number" && v >= 0);
  const sum = ph.reduce((a, [, v]) => a + v, 0);
  return (
    <section>
      <Head title="Start-up" meta={vm.readyMs != null ? <span>total <b className="vm-b">{fmtMs(vm.readyMs)}</b></span> : undefined} />
      {!ph.length ? <p className="vm-note">{vm.readyMs != null ? `Ready in ${fmtMs(vm.readyMs)}. No phase breakdown was reported.` : "No start-up timings were reported."}</p> : (
        <>
          <div className="vm-bar" role="img" aria-label="Start-up phases">
            {ph.map(([k, v], i) => <i key={k} title={`${pretty(k)} ${fmtMs(v)}`} style={{ flex: Math.max(v, 0.001) / (sum || 1), background: `var(${PHASE_COLS[i % PHASE_COLS.length]})` }} />)}
          </div>
          <ul className="vm-phases">
            {ph.map(([k, v], i) => (
              <li key={k}><span className="sw" style={{ background: `var(${PHASE_COLS[i % PHASE_COLS.length]})` }} /><span>{pretty(k)}</span><b>{fmtMs(v)}</b></li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

// ---------- side: this VM + events ----------
function Cube({ state }: { state: string }) {
  return (
    <svg className={`vm-cube st-${state}`} width="52" height="56" viewBox="0 0 52 56" aria-hidden="true">
      <polygon className="t" points="26,4 48,16 26,28 4,16" /><polygon className="l" points="4,16 26,28 26,52 4,40" /><polygon className="r" points="48,16 26,28 26,52 48,40" />
    </svg>
  );
}

function CopyId({ id }: { id: string }) {
  const toast = useToast();
  return (
    <span className="vm-id">
      <span title={id}>{id}</span>
      <button className="btn ghost sm" aria-label="Copy VM id" onClick={() => navigator.clipboard?.writeText(id).then(() => toast("VM id copied."), () => toast("Could not copy.", { kind: "err" }))}>Copy</button>
    </span>
  );
}

const evColor = (t: string) => (t.includes("lost") || t.includes("crash") || t.includes("error") ? "var(--err)" : t.includes("created") || t.includes("resumed") || t.includes("recovered") ? "var(--ok)" : t.includes("moved") ? "var(--info)" : "var(--muted)");
function evText(e: FlintEvent): string {
  const t = e.type.replace(/^vm\./, "");
  const x = e as Record<string, unknown>;
  if (t === "moved") return `Moved ${x.from_host ?? "?"} → ${x.to_host ?? "?"}${x.reason ? ` (${x.reason})` : ""}`;
  if (t === "state_changed") return `State ${pretty(String(x.from ?? "?")).toLowerCase()} → ${pretty(String(x.to ?? "?")).toLowerCase()}`;
  if (t === "lost" && x.reason) return `Lost: ${x.reason}`;
  return pretty(t);
}

function useVmEvents(id: string) {
  const cluster = useIsCluster();
  const live = useEvents().events;
  const hist = useQuery<FlintEvent[]>({
    queryKey: ["vm-events", id], enabled: cluster, retry: false, staleTime: Infinity,
    queryFn: async () => {
      // /v1/events is oldest-first with a 500 cap: page forward (bounded) so the newest are included.
      let since = 0, all: (FlintEvent & { seq?: number })[] = [];
      for (let i = 0; i < 6; i++) {
        const r = await api.get<{ events: (FlintEvent & { seq?: number })[] }>("/v1/events", { query: { vm_id: id, since } });
        all = all.concat(r.events);
        if (r.events.length < 500) break;
        since = Math.max(...r.events.map((e) => e.seq ?? 0));
      }
      return all;
    },
  });
  const items = useMemo(() => {
    const seen = new Set<string>(), out: { e: FlintEvent; k: string; fresh: boolean }[] = [];
    for (const e of live) if (e.vm_id === id) { const k = `${e.type}|${e.ts}`; if (!seen.has(k)) { seen.add(k); out.push({ e, k: e._k, fresh: false }); } }
    for (const e of hist.data ?? []) { const k = `${e.type}|${e.ts}`; if (!seen.has(k)) { seen.add(k); out.push({ e, k: "h" + k, fresh: false }); } }
    return out.sort((a, b) => b.e.ts - a.e.ts);
  }, [live, hist.data, id]);
  return { items, hist, cluster };
}

function Side({ vm }: { vm: VM }) {
  const cluster = useIsCluster();
  const { items, hist } = useVmEvents(vm.id);
  const rows: [string, React.ReactNode][] = [
    ["ID", <CopyId id={vm.id} />],
    ["Template", vm.template],
    ...(vm.tenant ? [["Tenant", vm.tenant] as [string, React.ReactNode]] : []),
    ...(cluster ? [["Host", vm.host || "—"] as [string, React.ReactNode]] : []),
    ["Backend", vm.backend || "—"],
    ["Agent healthy", vm.state === "running" ? (vm.agentHealthy ? "yes" : "no") : "—"],
    ["Created", vm.createdAt ? new Date(vm.createdAt * 1000).toLocaleString() : "—"],
    ...(cluster ? [["VM set / role", vm.vmset ? `${vm.vmset}${vm.raw.role ? ` · ${vm.raw.role}` : ""}` : "—"] as [string, React.ReactNode]] : []),
    ["Last checkpoint", vm.checkpointAt ? new Date(vm.checkpointAt * 1000).toLocaleString() : "never"],
  ];
  const feed: FeedItem[] = items.slice(0, 40).map(({ e, k }) => ({ key: k, time: fmtClock(e.ts), color: evColor(e.type), body: evText(e) }));
  return (
    <aside className="side" aria-label="VM facts and events">
      <section>
        <Sec l="A" title="This VM" />
        <div className="vm-card"><Cube state={vm.state} /><div className="who"><b title={vm.name}>{vm.name}</b>{vm.state === "paused" ? "Paused" : vm.state === "running" ? "Running" : pretty(vm.state)}</div></div>
        <dl className="vm-kv">{rows.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl>
      </section>
      <section className="vm-events">
        <Sec l="B" title="Events" meta={cluster && hist.isFetching ? "loading…" : undefined} />
        {feed.length ? <Feed items={feed} max={40} /> : <p className="vm-note">No events for this VM yet.{hist.isError ? ` History unavailable: ${errMsg(hist.error)}` : ""}</p>}
      </section>
    </aside>
  );
}

export default function Overview({ vm, running }: TabProps) {
  return (
    <div className="page-body vm-body">
      <div className="main-col">
        <MetricsStrip vm={vm} running={running} />
        <Processes vm={vm} running={running} />
        <Startup vm={vm} />
        <section>
          <Head title="Network" meta={<span className="dim">values hidden</span>} />
          <NetworkView vm={vm} />
        </section>
      </div>
      <Side vm={vm} />
    </div>
  );
}
