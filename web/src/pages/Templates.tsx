// Templates: list, import, delete, start a VM from one. Facts only; everything comes from /templates, /vms, /health, /events.
import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import { useEvents, usePlatform, useTemplates, useVms } from "../api/hooks";
import { deleteTemplate, importTemplate, refreshTemplates, useTemplate } from "../api/templates";
import type { Template } from "../api/types";
import { Dialog, Empty, Feed, Kpis, Sec, SkeletonRows, useToast, type FeedItem } from "../components/ui";
import { useKeyHints } from "../components/Shell";
import { fmtClock, fmtMiB, fmtSince } from "../kit/format";
import { usePageKeys } from "../kit/keys";
import "./templates.css";

const failed = (s: string) => s === "failed" || s === "error";
const errMsg = (e: unknown) => (e instanceof ApiError ? e.detail || `HTTP ${e.status}` : e instanceof Error ? e.message : String(e));
const tsMs = (t: number) => (t > 1e12 ? t : t * 1000);
const RECENT_MS = 10 * 60_000;

function TplStatus({ t }: { t: Template }) {
  if (t.status === "ready") return <span className="chip"><span className="dot" style={{ background: "var(--ok)" }} />Ready</span>;
  if (t.status === "building") return <span className="chip st-starting"><span className="tp-spin" aria-hidden="true" />Importing</span>;
  return (
    <span className="tp-fail" title={t.error}>
      <span className="chip st-error"><span className="dot" />{t.status === "error" ? "Error" : "Failed"}</span>
      {t.error && <small>{t.error}</small>}
    </span>
  );
}

export default function TemplatesPage() {
  const nav = useNavigate();
  const qc = useQueryClient();
  const toast = useToast();
  const plat = usePlatform().data;
  const cluster = plat?.mode === "cluster";
  const canImport = plat?.access !== "service"; // importing and deleting need an admin; the API says so if not
  const tpls = useTemplates();
  const vms = useVms();
  const { events } = useEvents();
  const [sel, setSel] = useState<string | null>(null);
  const [importing, setImporting] = useState(false);
  const [del, setDel] = useState<Template | null>(null);
  const watching = useRef(new Set<string>());

  const list = tpls.data ?? [];
  const cur = list.find((t) => t.template_id === sel) ?? list[0];
  const detail = useTemplate(cur?.template_id, cluster);
  const nReady = list.filter((t) => t.status === "ready").length;
  const nBuilding = list.filter((t) => t.status === "building").length;
  const usage = useMemo(() => {
    const m = new Map<string, { all: number; running: number }>();
    vms.data?.forEach((v) => { const u = m.get(v.template) ?? { all: 0, running: 0 }; u.all++; if (v.state === "running") u.running++; m.set(v.template, u); });
    return m;
  }, [vms.data]);

  // Poll faster while something is importing; toast when a watched import settles.
  useEffect(() => {
    if (!nBuilding) return;
    const id = window.setInterval(() => refreshTemplates(qc), 3000);
    return () => window.clearInterval(id);
  }, [nBuilding, qc]);
  const startVm = (id: string) => nav(`/templates?new=1&template=${encodeURIComponent(id)}`);
  useEffect(() => {
    if (!tpls.data) return;
    for (const id of [...watching.current]) {
      const t = tpls.data.find((x) => x.template_id === id);
      if (!t || t.status === "building") continue;
      watching.current.delete(id);
      if (t.status === "ready") toast(<><b>{t.name || id}</b> is ready</>, { action: "Start VM", onAction: () => startVm(id) });
      else toast(<><b>{t.name || id}</b> failed to import{t.error ? `: ${t.error}` : ""}</>, { kind: "err" });
    }
  }, [tpls.data, events[0]?._k]); // eslint-disable-line react-hooks/exhaustive-deps

  const move = (d: number) => { if (!list.length) return; const i = Math.max(0, Math.min(list.length - 1, list.findIndex((t) => t.template_id === cur?.template_id) + d)); setSel(list[i].template_id); };
  usePageKeys({ ArrowDown: () => move(1), ArrowUp: () => move(-1), j: () => move(1), k: () => move(-1), Enter: () => { if (cur?.status === "ready") startVm(cur.template_id); } });
  useKeyHints([{ k: "↑↓", label: "Move" }, { k: "↵", label: "Start VM" }, { k: "n", label: "New VM" }, { k: "/", label: "Search" }]);

  // ---- activity: template events + vm.created grouped by template (real events only) ----
  const tplName = (id: string) => list.find((t) => t.template_id === id)?.name || id;
  const feed = useMemo<FeedItem[]>(() => {
    const out: (FeedItem & { at: number })[] = [];
    const groups = new Map<string, { n: number; at: number }>();
    const cutoff = Date.now() - RECENT_MS;
    for (const e of events) {
      if (e.type === "template.ready" || e.type === "template.error") {
        const id = String(e.template_id ?? "");
        out.push({ key: e._k, at: tsMs(e.ts), time: fmtClock(e.ts), color: e.type === "template.ready" ? "var(--ok)" : "var(--err)", body: <><b>{tplName(id)}</b> {e.type === "template.ready" ? "is ready" : "failed to import"}</> });
      } else if (e.type === "vm.created" && tsMs(e.ts) >= cutoff) {
        const id = e.vm?.template_id ?? "default"; const g = groups.get(id) ?? { n: 0, at: 0 };
        g.n++; g.at = Math.max(g.at, tsMs(e.ts)); groups.set(id, g);
      }
    }
    groups.forEach((g, id) => out.push({ key: "g" + id, at: g.at, time: fmtClock(g.at), color: "var(--spark)", body: <><b>{g.n} VM{g.n > 1 ? "s" : ""}</b> started from <b>{tplName(id)}</b> in the last 10 min</> }));
    return out.sort((a, b) => b.at - a.at);
  }, [events, list]); // eslint-disable-line react-hooks/exhaustive-deps

  const kpis = [
    { label: "Templates ready", value: nReady, unit: `/ ${list.length}`, col: "var(--ok)" },
    { label: "Importing", value: nBuilding },
    ...(plat?.mode === "daemon" ? [{ label: "Golden snapshot ready", value: plat.health.golden_snapshot_ready ? "yes" : "no" }] : []),
  ];

  const u = cur ? usage.get(cur.template_id) : undefined;
  const hostClasses = detail.data?.host_classes ?? cur?.host_classes;

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Templates</h1>
          <div className="facts">
            {tpls.data ? <><b>{list.length} template{list.length === 1 ? "" : "s"}</b> · {nReady} ready{nBuilding > 0 && <> · <span className="sp">{nBuilding} importing</span></>}</> : "Loading…"}
            {plat?.health.backend_kind && <> · backend {plat.health.backend_kind}</>}
          </div>
        </div>
        <div className="row" style={{ marginLeft: "auto", gap: 16 }}>
          {tpls.data && <Kpis items={kpis} />}
          {canImport && <button className="btn pri" onClick={() => setImporting(true)}>Import an image</button>}
        </div>
      </header>

      <div className="page-body">
        <div className="main-col">
          <div className="scroll">
            <table className="tbl tp-tbl" aria-label="Templates">
              <thead><tr>
                <th scope="col">Name</th><th scope="col">ID</th><th scope="col">Status</th>
                <th scope="col">Image</th>
                {cluster ? <><th scope="col">Tenant</th><th scope="col">Import host</th></> : <th scope="col">Artifacts</th>}
                <th scope="col">Created</th><th scope="col" className="num">VMs</th><th scope="col"><span className="sr">Actions</span></th>
              </tr></thead>
              <tbody>
                {tpls.isPending && <SkeletonRows rows={5} cols={cluster ? 10 : 9} />}
                {list.map((t) => {
                  const us = usage.get(t.template_id);
                  return (
                    <tr key={t.template_id} className={t.template_id === cur?.template_id ? "sel" : ""} onClick={() => setSel(t.template_id)}>
                      <td><button className="tp-name" aria-pressed={t.template_id === cur?.template_id} onClick={() => setSel(t.template_id)}>{t.name || t.template_id}</button></td>
                      <td className="dim">{t.template_id}</td>
                      <td><TplStatus t={t} /></td>
                      <td className={t.image ? "" : "dim"} title={t.digest}>{t.image ? shortImage(t.image) : "—"}</td>
                      {cluster
                        ? <><td className={t.tenant ? "" : "dim"} title={t.tenant ? undefined : "Visible to all tenants"}>{t.tenant || "—"}</td><td className="dim">{t.build_host || "—"}</td></>
                        : <td>{t.artifacts && Object.keys(t.artifacts).length ? Object.entries(t.artifacts).map(([k, a]) => <span key={k} className="tp-art" title={`${k}: ${a.status}`}><span className="dot" style={{ background: a.status === "ready" ? "var(--ok)" : failed(a.status) ? "var(--err)" : "var(--blue)" }} />{k.replace(/^linux-/, "")}</span>) : <span className="dim">—</span>}</td>}
                      <td className="mut" title={t.created_at ? new Date(tsMs(t.created_at)).toLocaleString() : undefined}>{t.created_at ? `${fmtSince(t.created_at).split(" ")[0]} ago` : "—"}</td>
                      <td className="num" title={us ? `${us.running} running of ${us.all}` : "none"}>{us?.all ?? 0}</td>
                      <td>
                        <div className="tp-acts" onClick={(e) => e.stopPropagation()}>
                          <button className="btn sm" disabled={t.status !== "ready"} onClick={() => startVm(t.template_id)} aria-label={`Start a VM from ${t.name || t.template_id}`}>Start VM</button>
                          {canImport && <button className="btn sm danger" disabled={t.template_id === "default"} title={t.template_id === "default" ? "The default template cannot be deleted" : undefined} onClick={() => setDel(t)} aria-label={`Delete ${t.name || t.template_id}`}>Delete</button>}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {tpls.isError && <Empty title="Could not load templates" action={<button className="btn" onClick={() => tpls.refetch()}>Retry</button>}>{errMsg(tpls.error)}</Empty>}
            {tpls.data && !list.length && <Empty title="No templates" action={canImport ? <button className="btn pri" onClick={() => setImporting(true)}>Import an image</button> : undefined}>Import a container image, or run <code>flint templates import</code>.</Empty>}
          </div>
        </div>

        <aside className="side" aria-label="Template details and activity">
          <section>
            <Sec l="A" title="Selected" meta={cur ? cur.template_id : undefined} />
            {cur ? (
              <dl className="tp-facts">
                <dt>Name</dt><dd>{cur.name || cur.template_id}</dd>
                <dt>Status</dt><dd><TplStatus t={cur} /></dd>
                {cur.image && <><dt>Image</dt><dd className="tp-wrap">{cur.image}</dd><dt>Digest</dt><dd className="tp-wrap mut">{cur.digest || "—"}</dd></>}
                <dt>Created</dt><dd>{cur.created_at ? `${new Date(tsMs(cur.created_at)).toLocaleString()} (${fmtSince(cur.created_at)} ago)` : "—"}</dd>
                <dt>Root fs</dt><dd>{cur.rootfs_size_mb ? fmtMiB(cur.rootfs_size_mb) : "—"}</dd>
                <dt>VMs</dt><dd>{u ? `${u.all} (${u.running} running)` : "0"}</dd>
                {cluster && <><dt>Tenant</dt><dd>{cur.tenant || "all tenants"}</dd><dt>Import host</dt><dd>{cur.build_host || "—"}</dd>
                  <dt>Host classes</dt><dd>{detail.isPending ? <span className="sk" style={{ width: 80 }} /> : hostClasses?.length ? hostClasses.join(", ") : "none published"}</dd></>}
                {!cluster && cur.artifacts && Object.entries(cur.artifacts).map(([k, a]) => <Fragment key={k}><dt>{k.replace(/^linux-/, "")}</dt><dd>{a.status}{a.rootfs_size_mb ? ` · ${fmtMiB(a.rootfs_size_mb)}` : ""}</dd></Fragment>)}
                {cur.error && <><dt>Error</dt><dd className="errc tp-wrap">{cur.error}</dd></>}
              </dl>
            ) : <p className="tp-note">Select a template to see its facts.</p>}
          </section>
          <section className="tp-feedsec">
            <Sec l="B" title="Activity" meta="last 10 min of VM starts" />
            {feed.length ? <Feed items={feed} max={12} /> : <p className="tp-note">No template imports or VM starts seen since this page was opened.{plat?.mode === "daemon" ? " The daemon does not emit import events." : ""}</p>}
          </section>
        </aside>
      </div>

      {importing && <ImportDialog onClose={() => setImporting(false)} onStarted={(id) => { watching.current.add(id); setSel(id); refreshTemplates(qc); }} />}
      <Dialog open={!!del} onClose={() => setDel(null)} title="Delete template" sub={del ? <>Delete <b>{del.name || del.template_id}</b>? VMs already running from it keep running; no new VM can start from it.</> : undefined}
        actions={[{ label: "Cancel", kind: "ghost" }, { label: "Delete template", kind: "danger", id: "tp-del-confirm", onClick: async () => {
          if (!del) return;
          try { await deleteTemplate(del.template_id); toast(<>Deleted <b>{del.name || del.template_id}</b></>); refreshTemplates(qc); }
          catch (e) { toast(`Could not delete: ${errMsg(e)}`, { kind: "err" }); return false; }
        } }]} />
    </div>
  );
}

// "index.docker.io/library/python:3.12" reads better as "python:3.12".
const shortImage = (img: string) => img.replace(/^index\.docker\.io\/(library\/)?/, "");

function ImportDialog({ onClose, onStarted }: { onClose: () => void; onStarted: (id: string) => void }) {
  const toast = useToast();
  const [name, setName] = useState("");
  const [image, setImage] = useState("");
  const [size, setSize] = useState("2048");
  const [tenant, setTenant] = useState("");
  const [user, setUser] = useState("");
  const [pass, setPass] = useState("");
  const [err, setErr] = useState("");
  const sz = Number(size);
  const ok = name.trim().length > 0 && /^[a-zA-Z0-9][a-zA-Z0-9._\-/:]*(@sha256:[a-f0-9]{64})?$/.test(image.trim())
    && Number.isInteger(sz) && sz >= 64 && sz <= 16384 && (!tenant.trim() || /^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$/.test(tenant.trim()))
    && !!user.trim() === !!pass;
  return (
    <Dialog open onClose={onClose} title="Import an image" width={640}
      sub={<>Build and push the image with your usual tooling; Flint pulls it once, pins its digest and snapshots it so VMs boot in milliseconds.</>}
      actions={[{ label: "Cancel", kind: "ghost" }, { label: "Import", kind: "pri", id: "tp-import", disabled: !ok, onClick: async () => {
        setErr("");
        try {
          const r = await importTemplate({ name: name.trim(), image: image.trim(), rootfsSizeMb: sz, tenant: tenant.trim() || undefined,
            auth: user.trim() ? { username: user.trim(), password: pass } : undefined });
          toast(<>Importing <b>{r.template_id}</b>{r.build_host ? ` on ${r.build_host}` : ""}</>);
          onStarted(r.template_id);
        } catch (e) { setErr(errMsg(e)); toast(`Could not start the import: ${errMsg(e)}`, { kind: "err" }); return false; }
      } }]}>
      <div className="tp-form">
        <label htmlFor="tp-image">Image</label>
        <div><input id="tp-image" className="tp-in" placeholder="python:3.12-slim or ghcr.io/acme/app:1.4" value={image} onChange={(e) => setImage(e.target.value)} autoFocus spellCheck={false} /><div className="tp-hint">linux/amd64. A tag is pinned to the digest pulled now; import again to update.</div></div>
        <label htmlFor="tp-name">Name</label>
        <div><input id="tp-name" className="tp-in" placeholder="python-3.12" value={name} onChange={(e) => setName(e.target.value)} /><div className="tp-hint">The template id is derived from the name. Importing to an existing name replaces it once the new image is ready.</div></div>
        <label htmlFor="tp-size">Disk size</label>
        <div className="row"><input id="tp-size" className="tp-in tp-s" inputMode="numeric" value={size} onChange={(e) => setSize(e.target.value)} /><span className="mut">MiB, including free space for the VM</span></div>
        <label htmlFor="tp-tenant">Tenant</label>
        <div><input id="tp-tenant" className="tp-in tp-m" placeholder="optional" value={tenant} onChange={(e) => setTenant(e.target.value)} /><div className="tp-hint">Leave empty so every tenant can boot it; set one to keep it to that tenant.</div></div>
        <label htmlFor="tp-user">Registry login</label>
        <div><div className="row" style={{ gap: 8 }}><input id="tp-user" className="tp-in tp-m" placeholder="username (optional)" value={user} onChange={(e) => setUser(e.target.value)} autoComplete="off" /><input className="tp-in tp-m" type="password" placeholder="password or token" aria-label="Registry password or token" value={pass} onChange={(e) => setPass(e.target.value)} autoComplete="new-password" /></div><div className="tp-hint">For private registries. Used for this pull only; Flint never stores it.</div></div>
      </div>
      {err && <div className="tp-err" role="alert">{err}</div>}
    </Dialog>
  );
}
