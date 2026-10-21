// Load test: create N VMs from a template with a concurrency limit and watch the hosts fill.
import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { invalidateVMs, tenantsOf, useHosts, useIsAdmin, usePlatform, useQuota, useTemplates, useTokens, useVms } from "../api/hooks";
import { Dialog, Empty, Feed, Kpis, Sec, useToast, type FeedItem } from "../components/ui";
import { fmtClock } from "../kit/format";
import Floor, { type FloorHost, type FloorVM } from "./LoadTest/Floor";
import Hist from "./LoadTest/Hist";
import {
  forget, heatAt, heatRange, killAll, newRunId, readSaved, runLoad, stats,
  type LtResult, type RunCfg, type Saved,
} from "./LoadTest/engine";
import "./loadtest.css";

const NS = [10, 25, 50, 100], CS = [1, 4, 8, 16];
const f0 = (n: number) => Math.round(n).toLocaleString("en-US");
const RM = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

type Phase = "setup" | "running" | "done" | "cleaning";
interface RunState {
  id: string; cfg: RunCfg; results: LtResult[]; issued: number; stopping: boolean;
  wallMs: number | null; existing: FloorVM[]; startedWall: number;
}

function useSize<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [s, setS] = useState({ w: 0, h: 0 });
  useLayoutEffect(() => {
    const el = ref.current; if (!el) return;
    const upd = () => setS({ w: el.clientWidth, h: el.clientHeight });
    const ro = new ResizeObserver(upd); ro.observe(el); upd();
    return () => ro.disconnect();
  }, []);
  return [ref, s] as const;
}

function Elapsed({ since }: { since: number }) {
  const [, set] = useState(0);
  useEffect(() => { const t = window.setInterval(() => set((x) => x + 1), 200); return () => window.clearInterval(t); }, []);
  return <>{((Date.now() - since) / 1000).toFixed(1)} s</>;
}

export default function LoadTestPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const plat = usePlatform().data;
  const cluster = plat?.mode === "cluster";
  const admin = useIsAdmin();
  const vms = useVms();
  const tpls = useTemplates();
  const hosts = useHosts(admin);
  const tokens = useTokens(admin && cluster);

  const [n, setN] = useState(25), [conc, setConc] = useState(8), [internet, setInternet] = useState(false);
  const [tplId, setTplId] = useState(""), [tenant, setTenant] = useState("");
  const [phase, setPhase] = useState<Phase>("setup");
  const [run, setRun] = useState<RunState | null>(null);
  const [gone, setGone] = useState<Set<string>>(new Set());
  const [clean, setClean] = useState({ done: 0, total: 0 });
  const [confirm, setConfirm] = useState<string[] | null>(null);
  const [saved, setSaved] = useState<Saved | null>(readSaved);
  const ctl = useRef<{ stop: () => void } | null>(null);
  const lastRun = useRef<RunState | null>(null);

  useEffect(() => {
    const f = () => setSaved(readSaved());
    window.addEventListener("flint:loadtest", f);
    return () => window.removeEventListener("flint:loadtest", f);
  }, []);
  useEffect(() => () => ctl.current?.stop(), []); // leaving the page stops issuing new creates

  // ---- setup choices ----
  const ready = useMemo(() => (tpls.data ?? []).filter((t) => t.status === "ready"), [tpls.data]);
  useEffect(() => {
    if (ready.length && !ready.some((t) => t.template_id === tplId)) setTplId((ready.find((t) => t.template_id === "default") ?? ready[0]).template_id);
  }, [ready, tplId]);
  const tenants = useMemo(() => tenantsOf(vms.data, tokens.data), [vms.data, tokens.data]);
  useEffect(() => { if (cluster && !tenant && tenants.length) setTenant(tenants[0]); }, [cluster, tenant, tenants]);
  const quota = useQuota(tenant || undefined, cluster);
  const tplName = ready.find((t) => t.template_id === tplId)?.name ?? tplId;
  const cfg: RunCfg = { n, template: tplId, templateName: tplName, conc, tenant, internet, cluster };
  const summary = (c: RunCfg) => `${c.n} VMs · ${c.templateName} · ${c.conc} at a time`;

  // ---- floor model ----
  const hostKey = useCallback((h: string) => (cluster ? h : "local"), [cluster]);
  const liveExisting = useMemo<FloorVM[]>(
    () => (vms.data ?? []).map((v) => ({ id: v.id, host: hostKey(v.host) })).filter((v) => v.host),
    [vms.data, hostKey],
  );
  const existing = phase === "setup" || !run ? liveExisting : run.existing;
  const results = useMemo(() => run?.results ?? [], [run]);
  const oks = useMemo(() => results.filter((r) => r.ok), [results]);
  const floorHosts = useMemo<FloorHost[]>(() => {
    if (!cluster) return [{ id: "local", label: "this host", sub: plat?.health.backend_kind }];
    const base: FloorHost[] = (hosts.data ?? []).map((h) => ({ id: h.id, label: h.name || h.id, state: h.cordoned ? "cordoned" : h.state }));
    const seen = new Set(base.map((b) => b.id));
    [...existing.map((v) => v.host), ...oks.map((r) => r.host)].forEach((id) => { if (id && !seen.has(id)) { seen.add(id); base.push({ id, label: id }); } });
    return base;
  }, [cluster, hosts.data, existing, oks, plat?.health.backend_kind]);

  const heatVals = useMemo(() => oks.map((r) => r.readyMs ?? r.clientMs), [oks]);
  const range = useMemo(() => heatRange(heatVals), [heatVals]);
  const readyVals = useMemo(() => oks.map((r) => r.readyMs).filter((v): v is number => v != null), [oks]);
  const clientVals = useMemo(() => oks.map((r) => r.clientMs), [oks]);
  const st = useMemo(() => stats(readyVals), [readyVals]);
  const cst = useMemo(() => stats(clientVals), [clientVals]);
  const fails = results.filter((r) => !r.ok);
  const total = run?.cfg.n ?? n;
  const elapsedMs = run ? run.wallMs ?? results.reduce((m, r) => Math.max(m, r.at), 0) : 0;
  const rate = oks.length && elapsedMs > 200 ? oks.length / (elapsedMs / 1000) : null;
  const left = oks.filter((r) => !gone.has(r.id));

  // ---- run control ----
  const start = () => {
    if (!tplId || (cluster && !tenant)) return;
    const prev = lastRun.current;
    const base = new Map<string, FloorVM>();
    liveExisting.forEach((v) => base.set(v.id, v));
    prev?.results.filter((r) => r.ok && !gone.has(r.id)).forEach((r) => base.set(r.id, { id: r.id, host: hostKey(r.host) }));
    const id = newRunId(), c = { ...cfg };
    setGone(new Set());
    const rs: RunState = { id, cfg: c, results: [], issued: 0, stopping: false, wallMs: null, existing: [...base.values()], startedWall: Date.now() };
    lastRun.current = rs;
    setRun(rs); setPhase("running");
    const upd = (f: (s: RunState) => RunState) => setRun((s) => { if (!s || s.id !== id) return s; const x = f(s); lastRun.current = x; return x; });
    ctl.current = runLoad(c, id, {
      onResult: (r) => upd((s) => ({ ...s, results: [...s.results, r] })),
      onIssue: (k) => upd((s) => ({ ...s, issued: k })),
      onDone: (ms) => {
        upd((s) => ({ ...s, wallMs: ms }));
        setPhase((p) => (p === "running" ? "done" : p));
        invalidateVMs(qc); qc.invalidateQueries({ queryKey: ["hosts"] }); qc.invalidateQueries({ queryKey: ["quota"] });
      },
    });
  };
  const stop = () => { ctl.current?.stop(); setRun((s) => (s ? { ...s, stopping: true } : s)); };

  const cleanup = async (ids: string[]) => {
    setPhase("cleaning"); setClean({ done: 0, total: ids.length });
    const t0 = performance.now();
    const failed = new Set<string>();
    const r = await killAll(ids, (id, err) => {
      if (err) failed.add(id); else { setGone((g) => new Set(g).add(id)); forget([id]); }
      setClean((c) => ({ ...c, done: c.done + 1 }));
    });
    await new Promise((res) => setTimeout(res, RM() ? 0 : 520));
    invalidateVMs(qc); qc.invalidateQueries({ queryKey: ["hosts"] }); qc.invalidateQueries({ queryKey: ["quota"] });
    setGone((g) => { const x = new Set(g); failed.forEach((i) => x.delete(i)); return x; });
    lastRun.current = null; setRun(null); setPhase("setup");
    const secs = ((performance.now() - t0) / 1000).toFixed(1);
    if (r.failed) toast(`Removed ${ids.length - r.failed} of ${ids.length} VMs · ${r.failed} failed: ${r.firstError}`, { kind: "err" });
    else toast(`Removed ${ids.length} load-test VMs · ${secs} s`);
  };

  // ---- view pieces ----
  const [fbox, fsz] = useSize<HTMLDivElement>();
  const busy = phase === "running" || phase === "cleaning";
  const lead =
    phase === "setup" ? "Start N VMs from a template and measure time to ready."
    : phase === "running" ? <><b>{run?.stopping ? "Stopping" : "Running"}</b> · {summary(run!.cfg)}</>
    : phase === "cleaning" ? <><b>Cleaning up</b> · {clean.done} of {clean.total} removed</>
    : <><b>{run!.stopping ? "Stopped at" : "Done in"} {((run!.wallMs ?? elapsedMs) / 1000).toFixed(1)} s</b> · {summary(run!.cfg)}</>;

  const kpis = [
    { label: "Started", value: oks.length, unit: `/ ${total}` },
    { label: "Failed", value: <span className={fails.length ? "errc" : ""}>{run ? fails.length : "—"}</span> },
    { label: "Rate", value: rate == null ? "—" : rate.toFixed(1), unit: "VMs/s" },
    { label: "Start p50 (server)", value: st ? f0(st.median) : "—", unit: "ms" },
    { label: "Start p99 (server)", value: st ? f0(st.p99) : "—", unit: "ms" },
    { label: "Client p50", value: cst ? f0(cst.median) : "—", unit: "ms" },
  ];

  const feedItems: FeedItem[] = useMemo(() => {
    const items: FeedItem[] = [...results].reverse().slice(0, 40).map((r, i) => ({
      key: r.name, time: fmtClock(new Date(r.wall)), fresh: i === 0 && phase === "running",
      color: r.ok ? heatAt(range, r.readyMs ?? r.clientMs) : "var(--err)",
      body: r.ok ? <><b>{r.name}</b> ready on {cluster ? r.host : "this host"} · {f0(r.readyMs ?? r.clientMs)} ms</>
        : <><b>{r.name}</b> failed: {r.status || "error"} {r.detail}</>,
    }));
    if (phase === "done" && run) items.unshift({ key: "end", time: fmtClock(new Date(run.startedWall + (run.wallMs ?? 0))), color: "var(--ok)", body: `Run complete · ${oks.length} ready · ${fails.length} failed` });
    return items;
  }, [results, range, phase, run, oks.length, fails.length]);

  const perHost = useMemo(() => floorHosts.map((h) => {
    const mine = oks.filter((r) => r.host === h.id), s = stats(mine.map((r) => r.readyMs).filter((v): v is number => v != null));
    return { h, n: mine.length, p50: s?.median, p99: s?.p99 };
  }), [floorHosts, oks]);

  const failGroups = useMemo(() => {
    const m = new Map<string, number>();
    fails.forEach((r) => { const k = `${r.status || "error"} ${r.detail}`.trim(); m.set(k, (m.get(k) ?? 0) + 1); });
    return [...m.entries()].sort((a, b) => b[1] - a[1]);
  }, [fails]);

  const quotaFacts = (() => {
    if (!cluster || !tenant) return null;
    if (quota.isLoading) return <span className="dim">quota…</span>;
    if (!quota.data) return <span className="dim">quota unavailable</span>;
    const { max_vms: mx, vms: used } = quota.data;
    if (!mx) return <><b>{used}</b> VMs now · no quota limit</>;
    const free = Math.max(0, mx - used);
    return <><b>{used}</b> of {mx} VMs used · <b className={n > free ? "wrn" : ""}>{free}</b> free</>;
  })();

  const form = (
    <section>
      <Sec l="A" title="Setup" meta={<span>creates from this browser</span>} />
      <div className="panel lt-form">
        <div className="lt-frow">
          <div className="lt-fld"><span className="lbl" id="l-n">How many VMs</span>
            <span className="seg" role="group" aria-labelledby="l-n">{NS.map((v) => <button key={v} type="button" data-k="n" aria-pressed={v === n} onClick={() => setN(v)}>{v}</button>)}</span></div>
          <div className="lt-fld"><span className="lbl" id="l-c">Concurrency</span>
            <span className="seg" role="group" aria-labelledby="l-c">{CS.map((v) => <button key={v} type="button" data-k="c" aria-pressed={v === conc} onClick={() => setConc(v)}>{v}</button>)}</span></div>
          <div className="lt-fld"><label className="lbl" htmlFor="lt-tpl">Template</label>
            {tpls.isError ? <span className="lt-note errc">{(tpls.error as Error).message} <button className="btn sm" type="button" onClick={() => tpls.refetch()}>Retry</button></span>
              : <select id="lt-tpl" value={tplId} disabled={!ready.length} onChange={(e) => setTplId(e.target.value)}>
                {tpls.isLoading && <option>Loading…</option>}
                {!tpls.isLoading && !ready.length && <option>No ready templates</option>}
                {ready.map((t) => <option key={t.template_id} value={t.template_id}>{t.name}</option>)}
              </select>}</div>
        </div>
        <div className="lt-frow">
          {cluster && (
            <div className="lt-fld"><label className="lbl" htmlFor="lt-tenant">Tenant</label>
              {tenants.length
                ? <select id="lt-tenant" value={tenant} onChange={(e) => setTenant(e.target.value)}>{tenants.map((t) => <option key={t} value={t}>{t}</option>)}</select>
                : <input id="lt-tenant" type="text" value={tenant} placeholder="tenant name" onChange={(e) => setTenant(e.target.value.trim())} />}</div>
          )}
          <div className="lt-fld"><span className="lbl" id="l-i">Internet access</span>
            <span className="seg" role="group" aria-labelledby="l-i">
              <button type="button" aria-pressed={!internet} onClick={() => setInternet(false)}>Off</button>
              <button type="button" aria-pressed={internet} onClick={() => setInternet(true)}>On</button></span></div>
          {quotaFacts && <div className="lt-quota" id="lt-quota">{quotaFacts}</div>}
        </div>
        {saved && (
          <div className="lt-left" id="lt-left">
            <span><b>{saved.ids.length}</b> VMs left from the last run · {fmtClock(new Date(saved.at))}</span>
            <button className="btn sm danger" id="cleanup-left" type="button" onClick={() => setConfirm(saved.ids)}>Clean up {saved.ids.length} VMs left from the last run</button>
          </div>
        )}
        <div className="lt-fbar">
          <span className="est" id="summary"><b>{summary(cfg)}</b></span>
          <button className="btn pri" id="start" type="button" disabled={!tplId || (cluster && !tenant)} onClick={start}>Start load test</button>
        </div>
      </div>
    </section>
  );

  const floorMeta =
    phase === "setup" ? <span>{floorHosts.length} {cluster ? "hosts" : "host"} · {existing.length} VMs now</span>
    : phase === "running" ? <>
      <span className="chip st-running"><span className="dot" />{run!.stopping ? "Stopping" : "Starting VMs"}</span>
      <span id="progress">{results.length} / {run!.cfg.n} · <Elapsed since={run!.startedWall} /></span>
      <button className="btn sm" id="stop" type="button" disabled={run!.stopping} onClick={stop}>{run!.stopping ? "Stopping…" : "Stop"}</button></>
    : phase === "cleaning" ? <span className="chip st-starting"><span className="dot" />Removing VMs · {clean.done} / {clean.total}</span>
    : <>
      <button className="btn sm pri" id="again" type="button" onClick={start}>Run again</button>
      {left.length > 0 && <button className="btn sm" id="cleanup" type="button" onClick={() => setConfirm(left.map((r) => r.id))}>Clean up {left.length} VMs</button>}</>;

  const floor = (
    <section className="lt-floor-sec lt-sec">
      <Sec l={phase === "setup" ? "B" : "A"} title="Floor" meta={floorMeta} />
      <div className="lt-floorbox" ref={fbox} id="floor">
        {fsz.w > 0 && <Floor w={fsz.w} h={fsz.h} hosts={floorHosts} existing={existing} lt={results} range={range} gone={gone}
          expect={phase === "setup" ? n : run?.cfg.n ?? n} resetKey={run?.id ?? "setup"} />}
        {oks.length > 0 && <div className="lt-leg"><span>start time (server)</span><b>{f0(range.lo)} ms</b><span className="ramp" /><b>{f0(range.hi)} ms+</b></div>}
        <div className="lt-key"><span><i className="e" />existing</span><span><i className="r" />load test</span>{fails.length > 0 && <span><i className="f" />failed</span>}</div>
      </div>
      {fails.length > 0 && <FailedTray fails={fails} />}
    </section>
  );

  const histSec = phase !== "setup" && (
    <section>
      <Sec l="B" title="Start time (server)" meta={<span>readyMs · request to ready</span>} />
      <div className={`lt-histrow${phase === "done" ? " res" : ""}`}>
        <HistBox vals={readyVals} range={range} resetKey={run?.id ?? ""} />
        {phase === "done" && (
          <div className="lt-resg" id="results" aria-label="Results">
            {[["Min", st?.min], ["Avg", st?.avg], ["p50", st?.median], ["p95", st?.p95], ["p99", st?.p99], ["Max", st?.max]].map(([k, v]) => (
              <div key={k as string}><div className="k">{k}</div><div className="v">{v == null ? "—" : f0(v as number)}<i> ms</i></div></div>
            ))}
            <div><div className="k">Total time</div><div className="v">{((run!.wallMs ?? elapsedMs) / 1000).toFixed(1)}<i> s</i></div></div>
            <div><div className="k">Client p50</div><div className="v">{cst ? f0(cst.median) : "—"}<i> ms</i></div></div>
          </div>
        )}
      </div>
    </section>
  );

  // ---- side column ----
  let L = 0; const nextL = () => String.fromCharCode(67 + L++); // C, D, ...
  const side: ReactNode[] = [];
  if (phase === "setup") {
    if (cluster) side.push(
      <section key="h">
        <Sec l={nextL()} title="Hosts" meta={<span>now</span>} />
        {!admin ? <p className="lt-note">Host details need an admin token.</p>
          : hosts.isError ? <div className="lt-note errc">{(hosts.error as Error).message} <button className="btn sm" type="button" onClick={() => hosts.refetch()}>Retry</button></div>
          : hosts.isLoading ? <p className="lt-note">Loading hosts…</p>
          : !hosts.data?.length ? <Empty title="No hosts joined">Start one with <code>flint start --cluster</code>.</Empty>
          : <table className="tbl lt-sm" aria-label="Hosts"><thead><tr><th scope="col">Host</th><th scope="col">State</th><th scope="col" className="num">VMs</th><th scope="col" className="num">Max</th></tr></thead>
            <tbody>{hosts.data.map((h) => {
              const mx = h.max_vms_override > 0 ? Math.min(h.max_vms_override, h.capacity.max_vms) : h.capacity.max_vms;
              return <tr key={h.id}><td>{h.name || h.id}</td><td className="mut">{h.cordoned ? "cordoned" : h.state}</td><td className="num">{liveExisting.filter((v) => v.host === h.id).length}</td><td className="num">{mx}</td></tr>;
            })}</tbody></table>}
      </section>,
    ); else side.push(
      <section key="h">
        <Sec l={nextL()} title="This host" />
        <dl className="lt-kv"><dt>Backend</dt><dd>{plat?.health.backend_kind ?? "—"}</dd><dt>VMs now</dt><dd>{vms.data?.length ?? "—"}</dd>
          <dt>Golden snapshot</dt><dd className={plat?.health.golden_snapshot_ready ? "okc" : "wrn"}>{plat?.health.golden_snapshot_ready ? "ready" : "not ready"}</dd></dl>
      </section>,
    );
    side.push(<section key="t"><Sec l={nextL()} title="Template" />
      <dl className="lt-kv"><dt>Name</dt><dd>{tplName || "—"}</dd><dt>Ready templates</dt><dd>{ready.length}</dd></dl></section>);
  } else {
    if (cluster) {
      if (phase === "done") side.push(
        <section key="h">
          <Sec l={nextL()} title="Per host" meta={<span>ms</span>} />
          <table className="tbl lt-sm" id="perhost" aria-label="Per host"><thead><tr><th scope="col">Host</th><th scope="col" className="num">VMs</th><th scope="col" className="num">p50</th><th scope="col" className="num">p99</th></tr></thead>
            <tbody>{perHost.filter((p) => p.n > 0).map((p) => <tr key={p.h.id}><td>{p.h.label}</td><td className="num">{p.n}</td><td className="num">{p.p50 == null ? "—" : f0(p.p50)}</td><td className="num">{p.p99 == null ? "—" : f0(p.p99)}</td></tr>)}</tbody></table>
        </section>,
      ); else side.push(
        <section key="h">
          <Sec l={nextL()} title="Hosts" meta={<span>VMs ready</span>} />
          <div className="lt-hm" id="hostmeters">{perHost.map((p) => (
            <HostMeter key={p.h.id} id={p.h.label} n={p.n} total={run?.cfg.n ?? 1} />
          ))}</div>
        </section>,
      );
    }
    if (failGroups.length) side.push(
      <section key="f"><Sec l={nextL()} title="Failures" meta={<span>{fails.length} of {run?.cfg.n}</span>} />
        <div id="failures">{failGroups.map(([k, c]) => <div className="lt-fail" key={k}><span className="n">{c}</span><span>{k}</span></div>)}</div></section>,
    );
    side.push(
      <section key="s" className="lt-grow"><Sec l={nextL()} title="Starts" meta={phase === "running" ? <span className="chip st-running"><span className="dot" />live</span> : <span>latest first</span>} />
        <div style={{ flex: 1, minHeight: 0 }} id="feed">{feedItems.length ? <Feed items={feedItems} max={40} /> : <p className="lt-note">Waiting for the first VM…</p>}</div></section>,
    );
  }

  return (
    <div className="page lt">
      <header className="page-head">
        <div><h1>Load test</h1><div className="lead" id="lead">{lead}</div></div>
        <Kpis items={kpis} />
      </header>
      <div className="page-body">
        <div className="main-col">{phase === "setup" && form}{floor}{histSec}</div>
        <aside className="side" aria-label="Load test details">{side}</aside>
      </div>
      <Dialog open={!!confirm} onClose={() => setConfirm(null)} title={`Clean up ${confirm?.length ?? 0} VMs`}
        sub="These are the VMs the load test created. They are deleted for good."
        actions={[
          { label: "Cancel", kind: "ghost" },
          { id: "cleanup-confirm", label: `Delete ${confirm?.length ?? 0} VMs`, kind: "danger", onClick: () => { const ids = confirm ?? []; setConfirm(null); void cleanup(ids); } },
        ]}>
        <p className="lt-note">Deleted {RM() ? "" : "a few at a time"} (up to 8 at once). The cubes dissolve as each delete returns.</p>
      </Dialog>
      {busy && <span className="sr" aria-live="polite">{phase === "running" ? `${results.length} of ${total} responses` : `${clean.done} of ${clean.total} removed`}</span>}
    </div>
  );
}

function HostMeter({ id, n, total }: { id: string; n: number; total: number }) {
  return (
    <>
      <span className="mut">{id}</span>
      <div className="meter"><i style={{ width: `${Math.min(100, (n / Math.max(1, total)) * 100)}%` }} /></div>
      <span>{n}</span>
    </>
  );
}

function HistBox({ vals, range, resetKey }: { vals: number[]; range: { lo: number; hi: number }; resetKey: string }) {
  const [ref, { w }] = useSize<HTMLDivElement>();
  return <div className="lt-histbox" ref={ref}>{w > 0 && <Hist vals={vals} w={w} range={range} resetKey={resetKey} />}</div>;
}

/** Failed VMs are never placed on a host: they land in this tray as red outline cubes. */
function FailedTray({ fails }: { fails: LtResult[] }) {
  const [ref, { w }] = useSize<HTMLDivElement>();
  const S = 15, step = 22, per = Math.max(1, Math.floor((w - 24) / step));
  const shown = fails.slice(0, per);
  const hx = (cx: number, cy: number) => [[0, -S], [S * 0.866, -S / 2], [S * 0.866, S / 2], [0, S], [-S * 0.866, S / 2], [-S * 0.866, -S / 2]].map(([x, y]) => `${(cx + x * 0.62).toFixed(1)},${(cy + y * 0.62).toFixed(1)}`).join(" ");
  return (
    <div className="lt-tray" id="failed-tray">
      <span className="tl">Failed · {fails.length}</span>
      <div ref={ref} style={{ flex: 1, minWidth: 0 }}>
        {w > 0 && (
          <svg width={w} height={34} role="img" aria-label={`${fails.length} failed VMs`}>
            {shown.map((r, i) => {
              const cx = 12 + i * step, cy = 17, k = S * 0.62;
              return (
                <g className="fc" key={r.name}><title>{`${r.name}: ${r.status || "error"} ${r.detail}`}</title>
                  <polygon points={hx(cx, cy)} />
                  <path d={`M${cx - k * 0.866} ${cy - k / 2} L${cx} ${cy} L${cx + k * 0.866} ${cy - k / 2} M${cx} ${cy} V${cy + k}`} />
                </g>
              );
            })}
            {fails.length > per && <text className="more" x={12 + per * step} y={21}>+{fails.length - per}</text>}
          </svg>
        )}
      </div>
    </div>
  );
}
