import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useEvents, useHosts, usePlatform, useVms } from "../api/hooks";
import type { VM } from "../api/types";
import { Empty, Kpis, SkeletonRows, Tabs, useTick } from "../components/ui";
import { useKeyHints } from "../components/Shell";
import { usePageKeys } from "../kit/keys";
import { KillDialog, SelectionBar, useBulkPauseResume, useKill } from "./Vms/Bulk";
import { VmRow } from "./Vms/Row";
import { Activity, Placement, ThisHost } from "./Vms/Side";
import "./vms.css";

const byNewest = (a: VM, b: VM) => (b.createdAt ?? 0) - (a.createdAt ?? 0) || (a.id < b.id ? -1 : 1);
type Tab = "all" | "running" | "paused" | "failed";
const inTab = (v: VM, t: Tab) => t === "all" || (t === "failed" ? v.state === "error" || v.state === "lost" : v.state === t);

export default function VmsPage() {
  const plat = usePlatform().data;
  const cluster = plat?.mode === "cluster", admin = plat?.access === "admin";
  const q = useVms();
  const hostsQ = useHosts(cluster && admin);
  const { events } = useEvents();
  const nav = useNavigate();
  const [params, setParams] = useSearchParams();
  useTick(15000);

  const [tab, setTab] = useState<Tab>("all");
  const [text, setText] = useState("");
  const [cur, setCur] = useState<string | null>(null);
  const [selMode, setSelMode] = useState(false);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const anchor = useRef<string | null>(null);
  const [maxRss, setMaxRss] = useState(0);
  const onRss = useCallback((b: number) => setMaxRss((m) => (b > m ? b : m)), []);

  // stable order: newest first, independent of any metric
  const vms = useMemo(() => [...(q.data ?? [])].sort(byNewest), [q.data]);

  // new VMs get a short highlight
  const known = useRef<Set<string> | null>(null);
  const [fresh, setFresh] = useState<Set<string>>(new Set());
  useEffect(() => {
    if (!q.data) return;
    const ids = q.data.map((v) => v.id);
    if (!known.current) { known.current = new Set(ids); return; }
    const added = ids.filter((i) => !known.current!.has(i));
    ids.forEach((i) => known.current!.add(i));
    if (!added.length) return;
    setFresh((s) => new Set([...s, ...added]));
    window.setTimeout(() => setFresh((s) => { const n = new Set(s); added.forEach((i) => n.delete(i)); return n; }), 3000);
  }, [q.data]);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    vms.forEach((v) => { c[v.state] = (c[v.state] ?? 0) + 1; });
    return c;
  }, [vms]);
  const failed = (counts.error ?? 0) + (counts.lost ?? 0);

  const needle = text.trim().toLowerCase();
  const rows = useMemo(() => vms.filter((v) => inTab(v, tab) && (!needle || [v.name, v.id, v.tenant, v.host, v.template].some((s) => s.toLowerCase().includes(needle)))), [vms, tab, needle]);
  const ids = useMemo(() => rows.map((r) => r.id), [rows]);
  const curId = cur && ids.includes(cur) ? cur : ids[0] ?? null;

  const selVMs = useMemo(() => vms.filter((v) => sel.has(v.id)), [vms, sel]);

  const bulk = useBulkPauseResume();
  const kill = useKill();
  const [killOpen, setKillOpen] = useState(false);
  const [killTargets, setKillTargets] = useState<VM[]>([]);

  const open = useCallback((id: string) => nav(`/vms/${id}`), [nav]);
  const toggle = useCallback((id: string, shift = false) => {
    setSel((s) => {
      const n = new Set(s);
      if (shift && anchor.current && ids.includes(anchor.current)) {
        const a = ids.indexOf(anchor.current), b = ids.indexOf(id);
        ids.slice(Math.min(a, b), Math.max(a, b) + 1).forEach((i) => n.add(i));
      } else if (n.has(id)) n.delete(id); else n.add(id);
      return n;
    });
    anchor.current = id;
  }, [ids]);
  const clearSel = () => { setSel(new Set()); setSelMode(false); };

  const move = (d: number) => {
    if (!ids.length) return;
    const i = Math.max(0, Math.min(ids.length - 1, (curId ? ids.indexOf(curId) : 0) + d));
    setCur(ids[i]);
  };
  useEffect(() => { if (curId) document.querySelector(`tr[data-id="${curId}"]`)?.scrollIntoView({ block: "nearest" }); }, [curId]);

  usePageKeys({
    ArrowDown: () => move(1), j: () => move(1), ArrowUp: () => move(-1), k: () => move(-1),
    Enter: () => curId && open(curId),
    c: () => curId && nav(`/vms/${curId}/shell`),
    space: () => { if (selMode) clearSel(); else setSelMode(true); },
    "*": () => { setSelMode(true); setSel(new Set(ids)); },
    x: () => { if (selMode && curId) toggle(curId); },
    Escape: () => { if (selMode || sel.size) clearSel(); },
  });
  useKeyHints(selMode
    ? [{ k: "↑↓", label: "move" }, { k: "x", label: "tick row" }, { k: "*", label: "select all shown" }, { k: "esc", label: "clear" }]
    : [{ k: "↑↓", label: "move" }, { k: "↵", label: "open" }, { k: "c", label: "connect" }, { k: "space", label: "select several" }]);

  useEffect(() => {
    document.body.classList.toggle("vms-bar", sel.size > 0);
    return () => document.body.classList.remove("vms-bar");
  }, [sel.size]);

  const newVm = () => { const p = new URLSearchParams(params); p.set("new", "1"); setParams(p); };

  // ---------- head ----------
  const total = vms.length;
  const ready = vms.map((v) => v.readyMs).filter((x): x is number => x != null).sort((a, b) => a - b);
  const p50 = ready.length ? ready[Math.floor(ready.length / 2)] : null;
  const h = plat?.health;
  const kpis = [
    { label: "Running", value: counts.running ?? 0, unit: `/ ${total}` },
    { label: "Paused", value: counts.paused ?? 0 },
    { label: "Start time p50", value: p50 == null ? "—" : Math.round(p50), unit: p50 == null ? undefined : "ms" },
    ...(cluster && admin && h?.hosts != null ? [{ label: "Hosts ready", value: h.hosts_ready ?? 0, unit: `/ ${h.hosts}` }] : []),
  ];
  const facts: ReactNode[] = [];
  if (counts.running) facts.push(<b key="r">{counts.running} running</b>);
  if (counts.paused) facts.push(<span key="p">{counts.paused} paused</span>);
  if (counts.starting) facts.push(<span key="s">{counts.starting} starting</span>);
  if (counts.migrating) facts.push(<span key="m">{counts.migrating} migrating</span>);
  if (counts.recovering) facts.push(<span key="rc">{counts.recovering} recovering</span>);
  if (counts.stopped) facts.push(<span key="st">{counts.stopped} stopped</span>);
  if (failed) facts.push(<span key="f" className="errc">{failed} failed</span>);

  const cols = 5 + (cluster ? 2 : 0) + (selMode ? 1 : 0);
  const empty = !q.isLoading && !q.isError && total === 0;

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>VMs</h1>
          <div className="facts">{q.data ? (facts.length ? facts.flatMap((f, i) => (i ? [" · ", f] : [f])) : "no VMs") : "loading…"}</div>
        </div>
        <Kpis items={kpis} />
      </header>

      <div className="page-body">
        <div className="main-col">
          {empty ? (
            <Empty title="No VMs yet" action={<button className="btn pri" onClick={newVm}>New VM</button>}>
              <span>Create one here, or from code:</span>
              <code>from flint import Sandbox; Sandbox()</code>
            </Empty>
          ) : (
            <>
              <div className="vms-tbar">
                <Tabs active={tab} onChange={(t) => setTab(t as Tab)} tabs={[
                  { id: "all", label: "All", count: total }, { id: "running", label: "Running", count: counts.running ?? 0 },
                  { id: "paused", label: "Paused", count: counts.paused ?? 0 }, { id: "failed", label: "Failed", count: failed },
                ]} />
                <span className="vms-hint"><span className="kbd">space</span> select several</span>
                <label className="vms-search">
                  <span className="dim" aria-hidden="true">⌕</span>
                  <input value={text} placeholder="Filter VMs" autoComplete="off" aria-label="Filter VMs"
                    onChange={(e) => setText(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Escape") { setText(""); (e.target as HTMLInputElement).blur(); } if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }} />
                </label>
              </div>
              <div className="vms-scroll">
                <table className="tbl vms-tbl" aria-label="VMs">
                  <thead>
                    <tr>
                      {selMode && <th scope="col" className="vms-cb">
                        <button className="cb" role="checkbox" aria-label={`Select all ${ids.length} shown`} aria-checked={ids.length > 0 && ids.every((i) => sel.has(i))}
                          onClick={() => setSel(ids.every((i) => sel.has(i)) ? new Set() : new Set(ids))} />
                      </th>}
                      <th scope="col">VM</th>
                      {cluster && <th scope="col">Tenant</th>}
                      {cluster && <th scope="col">Host</th>}
                      <th scope="col">CPU</th><th scope="col">Memory</th>
                      <th scope="col" className="num">Up</th><th scope="col" className="num">Start</th>
                    </tr>
                  </thead>
                  <tbody>
                    {q.isLoading && <SkeletonRows rows={9} cols={cols} />}
                    {rows.map((v, i) => (
                      <VmRow key={v.id} vm={v} index={i} cluster={cluster} cursor={v.id === curId} checked={sel.has(v.id)} selMode={selMode}
                        fresh={fresh.has(v.id)} maxRss={maxRss} onRss={onRss} onOpen={open} onCursor={setCur} onToggle={toggle} />
                    ))}
                    {q.data && !rows.length && <tr className="vms-none"><td colSpan={cols}>No VMs match{needle ? ` “${text.trim()}”` : ""}.</td></tr>}
                  </tbody>
                </table>
                {q.isError && (
                  <Empty title="Couldn't load VMs" action={<button className="btn" onClick={() => void q.refetch()}>Retry</button>}>
                    {q.error instanceof Error ? q.error.message : "Request failed"}
                  </Empty>
                )}
              </div>
            </>
          )}
        </div>

        <aside className="side" aria-label="Activity">
          {cluster && admin && <Placement hosts={hostsQ.data} vms={vms} sel={curId} />}
          <Activity letter={cluster && admin ? "B" : "A"} events={events} vms={vms} />
          {!cluster && <ThisHost health={h} />}
        </aside>
      </div>

      {sel.size > 0 && (
        <SelectionBar sel={selVMs} busy={bulk.busy}
          onPause={() => void bulk.run("pause", selVMs.filter((v) => v.state === "running"))}
          onResume={() => void bulk.run("resume", selVMs.filter((v) => v.state === "paused"))}
          onKill={() => { setKillTargets(selVMs); setKillOpen(true); }}
          onClear={clearSel} />
      )}
      <KillDialog open={killOpen} onClose={() => setKillOpen(false)} targets={killTargets} kill={kill} />
    </div>
  );
}
