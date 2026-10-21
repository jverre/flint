// New VM drawer. POST /vms blocks until the VM is ready, so the "flight" is an indeterminate
// cube + elapsed ticker; the real phase breakdown (vm.timings) is revealed when the response arrives.
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import { createVM, invalidateVMs, tenantsOf, useIsCluster, useQuota, useTemplates, useVms, type CreateOpts } from "../api/hooks";
import type { VM } from "../api/types";
import { Dialog, Sec, useToast, type DialogAction } from "../components/ui";
import { fmtMiB, fmtMs, fmtSince } from "../kit/format";
import { Cube, type CubeState } from "./NewVm/Cube";
import { Phases } from "./NewVm/Phases";
import "./newvm.css";

const TENANT_RE = /^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$/;
const OTHER = "__other__";
const MAX_COUNT = 20, CONCURRENCY = 4;

interface Item { st: "queued" | "starting" | "ok" | "err"; vm?: VM; detail?: string; ms?: number }

const median = (a: number[]) => { if (!a.length) return null; const s = [...a].sort((x, y) => x - y); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
const errDetail = (e: unknown) => (e instanceof ApiError ? `${e.detail || "request failed"} (HTTP ${e.status})` : e instanceof Error ? e.message : String(e));

async function copyText(t: string) {
  try { await navigator.clipboard.writeText(t); return true; } catch { /* insecure context or denied: fall back */ }
  const ta = document.createElement("textarea"); ta.value = t; ta.style.position = "fixed"; ta.style.opacity = "0"; document.body.appendChild(ta); ta.select();
  try { return document.execCommand("copy"); } finally { ta.remove(); }
}

export default function NewVmDrawer({ onClose, template }: { onClose: () => void; template?: string }) {
  const cluster = useIsCluster();
  const nav = useNavigate();
  const qc = useQueryClient();
  const toast = useToast();
  const vms = useVms();
  const tpls = useTemplates();
  const tenants = useMemo(() => tenantsOf(vms.data), [vms.data]);

  // ---- form state ----
  const [tenantSel, setTenantSel] = useState("");
  const [other, setOther] = useState("");
  const [tpl, setTpl] = useState(template ?? "default");
  const [name, setName] = useState("");
  const [internet, setInternet] = useState(true);
  const [autoStop, setAutoStop] = useState(false);
  const [minutes, setMinutes] = useState("30");
  const [policy, setPolicy] = useState<"kill" | "pause">("kill");
  const [count, setCount] = useState(1);
  useEffect(() => { if (template) setTpl(template); }, [template]);

  const sel = tenantSel || tenants[0] || OTHER;
  const tenant = sel === OTHER ? other.trim() : sel;
  const tenantOk = TENANT_RE.test(tenant);
  const quota = useQuota(cluster && tenantOk ? tenant : undefined);
  const mins = Number(minutes);
  const stopOk = !autoStop || (Number.isInteger(mins) && mins > 0 && mins <= 60 * 24 * 30);
  const tplRow = tpls.data?.find((t) => t.template_id === tpl);
  const tplOk = !tpls.data || tplRow?.status === "ready";
  const formOk = (!cluster || tenantOk) && stopOk && tplOk && count >= 1 && count <= MAX_COUNT;

  // ---- run state ----
  const [items, setItems] = useState<Item[] | null>(null);
  const [t0, setT0] = useState(0);
  const [tEnd, setTEnd] = useState<number | null>(null);
  const [now, setNow] = useState(0);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const settled = !!items && items.every((i) => i.st === "ok" || i.st === "err");
  useEffect(() => {
    if (!items || tEnd != null) return;
    const id = window.setInterval(() => setNow(performance.now()), 50);
    return () => window.clearInterval(id);
  }, [items, tEnd]);
  const elapsed = items ? Math.max(0, (tEnd ?? (now || performance.now())) - t0) : 0;

  const optsFor = (i: number): CreateOpts => ({
    template: tpl,
    tenant: cluster ? tenant : undefined,
    name: cluster && name.trim() ? (count > 1 ? `${name.trim()}-${i + 1}` : name.trim()) : undefined,
    allowInternet: internet,
    ...(autoStop ? { timeoutSeconds: Math.round(mins * 60), timeoutPolicy: policy } : {}),
  });

  const start = () => {
    const n = count, begin = performance.now();
    setItems(Array.from({ length: n }, () => ({ st: "queued" as const })));
    setT0(begin); setTEnd(null); setNow(begin);
    const patch = (i: number, p: Item) => setItems((cur) => (cur ? cur.map((x, j) => (j === i ? p : x)) : cur));
    let next = 0, ok = 0, bad = 0;
    const worker = async () => {
      while (next < n) {
        const i = next++, s = performance.now();
        patch(i, { st: "starting" });
        try {
          const vm = await createVM(optsFor(i), cluster);
          ok++; patch(i, { st: "ok", vm, ms: performance.now() - s });
        } catch (e) { bad++; patch(i, { st: "err", detail: errDetail(e), ms: performance.now() - s }); }
        invalidateVMs(qc);
      }
    };
    void Promise.all(Array.from({ length: Math.min(CONCURRENCY, n) }, worker)).then(() => {
      qc.invalidateQueries({ queryKey: ["quota"] });
      if (mounted.current) setTEnd(performance.now());
      else toast(bad ? `${ok} of ${n} VMs started, ${bad} failed` : n > 1 ? `${n} VMs started` : "VM started", { kind: bad ? "err" : undefined });
    });
  };
  const reset = () => { setItems(null); setTEnd(null); };

  const copy = async () => {
    const args = [`template_id="${tpl}"`, ...(cluster && tenant ? [`tenant="${tenant}"`] : []), ...(internet ? [] : ["allow_internet_access=False"])].join(", ");
    const stop = autoStop && stopOk ? `.set_timeout(${Math.round(mins * 60)}, "${policy}")` : "";
    const nm = cluster && name.trim() ? `# "${name.trim()}": the SDK has no name argument; it is the "name" label in the HTTP API\n` : "";
    const code = "from flint import Sandbox\n\n" + nm + (count > 1
      ? `sbs = [Sandbox(${args}) for _ in range(${count})]` + (stop ? `\nfor sb in sbs:\n    sb${stop}` : "")
      : `sb = Sandbox(${args})` + (stop ? `\nsb${stop}` : ""));
    toast((await copyText(code)) ? "Copied the Python command" : "Could not copy to the clipboard");
  };

  // ---- derived run results ----
  const single = items?.length === 1 ? items[0] : null;
  const oks = (items ?? []).filter((i) => i.st === "ok" && i.vm);
  const readies = oks.map((i) => i.vm!.readyMs).filter((x): x is number => x != null);
  const nOk = oks.length, nErr = (items ?? []).filter((i) => i.st === "err").length;
  const go = (path: string) => { nav(path); return false as const; };

  let title = "New VM", sub: string | undefined;
  if (items) {
    if (!settled) { title = items.length > 1 ? `Starting ${items.length} VMs` : "Starting your VM"; sub = "Waiting for the host to answer. You can hide this; it keeps starting."; }
    else if (nErr === items.length) { title = items.length > 1 ? "No VM started" : "Could not start the VM"; }
    else { title = items.length > 1 ? `${nOk} of ${items.length} VMs started` : "VM started"; }
  } else sub = cluster ? "Pick a tenant and a template. Everything else is optional." : "Pick a template. Everything else is optional.";

  // ---- footer ----
  let actions: DialogAction[];
  if (!items) actions = [{ label: "Cancel", kind: "ghost" }, { label: count > 1 ? `Start ${count} VMs` : "Start VM", kind: "pri", id: "nv-start", disabled: !formOk, onClick: () => { start(); return false; } }];
  else if (!settled) actions = [{ label: "Hide", kind: "ghost" }];
  else if (nOk === 0) actions = [{ label: "Close", kind: "ghost" }, { label: "Edit and retry", kind: "pri", onClick: () => { reset(); return false; } }];
  else if (single?.vm) actions = [
    { label: "Start another", kind: "ghost", onClick: () => { reset(); return false; } },
    { label: "Open", id: "nv-open", onClick: () => go(`/vms/${single.vm!.id}`) },
    { label: "Connect", kind: "pri", id: "nv-connect", onClick: () => go(`/vms/${single.vm!.id}/shell`) },
  ];
  else actions = [{ label: "Start another", kind: "ghost", onClick: () => { reset(); return false; } }, { label: "Done", kind: "pri" }];

  const foot = !items
    ? <><button className="nv-link" id="nv-copy" onClick={copy}>Copy as command</button><span className="nv-sum">{count} × {tpl}{cluster && tenant ? ` → ${tenant}` : ""}</span></>
    : null;

  let letter = 0; const L = () => "ABCD"[letter++];
  return (
    <Dialog open onClose={onClose} title={title} sub={sub} side width={720} actions={actions} foot={foot}>
      <div className="nv">
        {!items && (
          <>
            {cluster && (
              <section>
                <Sec l={L()} title="Tenant" meta="required" />
                <div className="nv-inline">
                  <select className="nv-in" aria-label="Tenant" style={{ minWidth: 200 }} value={sel} onChange={(e) => setTenantSel(e.target.value)}>
                    {tenants.map((t) => <option key={t} value={t}>{t}</option>)}
                    <option value={OTHER}>Other…</option>
                  </select>
                  {sel === OTHER && <input className={`nv-in w${other && !tenantOk ? " bad" : ""}`} aria-label="Tenant name" placeholder="tenant name" value={other} onChange={(e) => setOther(e.target.value)} autoFocus />}
                </div>
                {sel === OTHER && other && !tenantOk && <div className="nv-hint bad">Letters, digits, dot, dash and underscore; must start with a letter or digit; up to 63 characters.</div>}
                {quota.data && (
                  <div className="nv-quota" aria-live="polite">
                    {quota.data.max_vms > 0 && <span className="nv-qm" aria-hidden="true"><i className={quota.data.vms + count > quota.data.max_vms ? "w" : ""} style={{ width: `${Math.min(100, (quota.data.vms / quota.data.max_vms) * 100)}%` }} /></span>}
                    <span>{quota.data.max_vms > 0 ? `${quota.data.vms} of ${quota.data.max_vms} VMs used` : `${quota.data.vms} VMs used · no limit`}</span>
                  </div>
                )}
                {quota.data && quota.data.max_vms > 0 && quota.data.vms + count > quota.data.max_vms && <div className="nv-hint warn">Starting {count} would go over this tenant's limit of {quota.data.max_vms}.</div>}
              </section>
            )}

            <section>
              <Sec l={L()} title="Template" meta={tpls.data ? `${tpls.data.filter((t) => t.status === "ready").length} ready` : undefined} />
              {tpls.isPending ? <div className="nv-rows">{[0, 1, 2].map((i) => <div key={i} className="nv-ro" style={{ cursor: "default" }}><span /><span className="sk" style={{ width: `${40 + i * 15}%` }} /><span /></div>)}</div>
                : tpls.isError ? <div className="nv-err"><b>Could not load templates</b>{errDetail(tpls.error)} <button className="nv-link" onClick={() => tpls.refetch()}>Retry</button></div>
                : (
                  <div className="nv-rows" role="radiogroup" aria-label="Template">
                    {tpls.data.map((t) => {
                      const ready = t.status === "ready", on = tpl === t.template_id;
                      return (
                        <button key={t.template_id} className={`nv-ro${on ? " on" : ""}`} role="radio" aria-checked={on} disabled={!ready} title={t.error || undefined} onClick={() => setTpl(t.template_id)}>
                          <span className="nv-rd" />
                          <span className="n">{t.name || t.template_id}{t.name && t.name !== t.template_id && <span className="d" style={{ marginLeft: 8 }}>{t.template_id}</span>}</span>
                          <span className="d">
                            {ready ? [t.rootfs_size_mb ? fmtMiB(t.rootfs_size_mb) : null, t.created_at ? `created ${fmtSince(t.created_at)} ago` : null].filter(Boolean).join(" · ")
                              : <span className={t.status === "building" ? "sp" : "errc"}>{t.status === "building" ? "building…" : t.status === "error" || t.status === "failed" ? "build failed" : t.status}</span>}
                          </span>
                        </button>
                      );
                    })}
                  </div>
                )}
              {tpls.data && !tplRow && <div className="nv-hint bad">Template “{tpl}” does not exist.</div>}
            </section>

            <section>
              <Sec l={L()} title="Options" />
              <div className="nv-fields">
                {cluster && <><label htmlFor="nv-name">Name</label><div><input id="nv-name" className="nv-in w" placeholder="optional" value={name} onChange={(e) => setName(e.target.value)} maxLength={63} /></div></>}
                <span className="lb" id="nv-net-l">Internet access</span>
                <div className="nv-inline"><button className="nv-switch" role="switch" aria-checked={internet} aria-labelledby="nv-net-l" onClick={() => setInternet(!internet)} /><span className="mut">{internet ? "On" : "Off: no outbound network"}</span></div>
                <span className="lb" id="nv-stop-l">Auto-stop</span>
                <div>
                  <div className="nv-inline">
                    <button className="nv-switch" role="switch" aria-checked={autoStop} aria-labelledby="nv-stop-l" onClick={() => setAutoStop(!autoStop)} />
                    {!autoStop && <span className="mut">Off</span>}
                    {autoStop && (
                      <>
                        <span className="mut">after</span>
                        <input className={`nv-in s${stopOk ? "" : " bad"}`} aria-label="Minutes until auto-stop" inputMode="numeric" value={minutes} onChange={(e) => setMinutes(e.target.value)} />
                        <span className="mut">min, then</span>
                        <span className="seg" role="radiogroup" aria-label="When the timeout fires">
                          {(["kill", "pause"] as const).map((p) => <button key={p} role="radio" aria-checked={policy === p} className={policy === p ? "on" : ""} onClick={() => setPolicy(p)}>{p === "kill" ? "Kill" : "Pause"}</button>)}
                        </span>
                      </>
                    )}
                  </div>
                  {autoStop && !stopOk && <div className="nv-hint bad">Enter a whole number of minutes.</div>}
                </div>
              </div>
            </section>

            <section>
              <Sec l={L()} title="How many" meta={`1 to ${MAX_COUNT}`} />
              <div className="nv-inline">
                <span className="nv-step">
                  <button aria-label="Fewer" disabled={count <= 1} onClick={() => setCount(Math.max(1, count - 1))}>−</button>
                  <input aria-label="How many VMs" inputMode="numeric" value={count} onChange={(e) => { const n = parseInt(e.target.value.replace(/\D/g, ""), 10); setCount(Number.isFinite(n) ? Math.min(MAX_COUNT, Math.max(1, n)) : 1); }} />
                  <button aria-label="More" disabled={count >= MAX_COUNT} onClick={() => setCount(Math.min(MAX_COUNT, count + 1))}>+</button>
                </span>
                {count > 1 && <span className="nv-note">Started {CONCURRENCY} at a time.</span>}
              </div>
            </section>
          </>
        )}

        {items && single && (
          <div className="nv-hero" aria-live="polite">
            <Cube state={single.st === "ok" ? "lit" : single.st === "err" ? "err" : "fly"} size={120} label={single.st === "ok" ? "VM ready" : single.st === "err" ? "Start failed" : "Starting"} />
            {single.st === "ok" && single.vm ? (
              <>
                <div className="nv-tick">Started in<span className="n">{single.vm.readyMs == null ? "—" : single.vm.readyMs.toFixed(single.vm.readyMs >= 100 ? 0 : 1)}<i>ms</i></span></div>
                <div className="nv-tick2">server-measured · round trip from this browser {fmtMs(single.ms)}</div>
                <div className="nv-facts">
                  <span>VM <b><Link to={`/vms/${single.vm.id}`}>{single.vm.name}</Link></b></span>
                  {single.vm.host && <span>host <b>{single.vm.host}</b></span>}
                  {single.vm.backend && <span>backend <b>{single.vm.backend}</b></span>}
                  {single.vm.tenant && <span>tenant <b>{single.vm.tenant}</b></span>}
                </div>
              </>
            ) : single.st === "err" ? (
              <div className="nv-err" role="alert" style={{ marginTop: 14, alignSelf: "stretch" }}><b>The server refused or failed the request</b>{single.detail}</div>
            ) : (
              <>
                <div className="nv-tick">Starting<span className="n">{Math.round(elapsed)}<i>ms</i></span></div>
                <p className="nv-note">The server answers once the VM is ready, so there are no live phases. The real breakdown appears when it arrives.</p>
              </>
            )}
          </div>
        )}
        {items && single?.st === "ok" && single.vm && <section><Sec l="A" title="Where the time went" meta="from the create response" /><Phases timings={single.vm.timings} readyMs={single.vm.readyMs} /></section>}

        {items && !single && (
          <>
            <div className="nv-batch" aria-live="polite">
              <div className="nv-cubes">
                {items.map((it, i) => <Cube key={i} size={items.length > 10 ? 30 : 38} state={cubeOf(it)} label={`VM ${i + 1}: ${it.st}`} />)}
              </div>
              <div className="nv-tick2">{nOk + nErr} of {items.length} answered · {fmtMs(elapsed)}</div>
            </div>
            {settled && nOk > 0 && (
              <div className="nv-sumline">
                <span>ready time p50 <b>{fmtMs(median(readies))}</b></span>
                <span>max <b>{fmtMs(readies.length ? Math.max(...readies) : null)}</b></span>
                <span>wall clock <b>{fmtMs(elapsed)}</b></span>
                {nErr > 0 && <span className="errc">{nErr} failed</span>}
              </div>
            )}
            <section>
              <Sec l="A" title="VMs" meta={`${nOk} started${nErr ? ` · ${nErr} failed` : ""}`} />
              <div className="nv-list">
                {items.map((it, i) => (
                  <div className="nv-li" key={i}>
                    <span className={`st ${it.st === "ok" ? "ok" : it.st === "err" ? "err" : it.st === "starting" ? "starting" : ""}`} aria-hidden="true" />
                    {it.vm ? <Link className="n" to={`/vms/${it.vm.id}`}>{it.vm.name}</Link> : <span className="n">{optsFor(i).name ?? `VM ${i + 1}`}</span>}
                    <span className="r" title="server ready time">{it.vm ? fmtMs(it.vm.readyMs) : it.st === "queued" ? "queued" : it.st === "starting" ? "starting" : "failed"}</span>
                    <span className="r dim" title="round trip from this browser">{it.ms != null ? fmtMs(it.ms) : ""}</span>
                    {it.detail && <span className="det">{it.detail}</span>}
                  </div>
                ))}
              </div>
            </section>
          </>
        )}
      </div>
    </Dialog>
  );
}

const cubeOf = (i: Item): CubeState => (i.st === "ok" ? "lit" : i.st === "err" ? "err" : i.st === "starting" ? "fly" : "queued");
