// App frame: full-height sidebar, centred search, page outlet.
import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { recentVMs, signOut, useHosts, useIsAdmin, usePlatform, useSession, useTemplates, useVms } from "../api/hooks";
import { getToken, setToken } from "../api/client";
import { cubeSVG, token } from "../kit/svg";
import { dispatchPageKey, isTyping, keyName, type KeyHint } from "../kit/keys";
import { stateLabel } from "../kit/format";
import { Dialog, StateChip } from "./ui";
import NewVmDrawer from "../pages/NewVm";

// ---------- page key hints (the "?" list) ----------
const HintCtx = createContext<(h: KeyHint[]) => void>(() => {});
export function useKeyHints(hints: KeyHint[]) {
  const set = useContext(HintCtx);
  const sig = hints.map((h) => h.k + h.label).join("|");
  useEffect(() => { set(hints); return () => set([]); }, [sig]); // eslint-disable-line react-hooks/exhaustive-deps
}

const GLOBAL: KeyHint[] = [
  { k: "/", label: "Search everything" }, { k: "n", label: "New VM" }, { k: "g f", label: "Go to VMs" }, { k: "g t", label: "Go to tenants" },
  { k: "g h", label: "Go to hosts" }, { k: "g p", label: "Go to templates" }, { k: "g l", label: "Go to load test" }, { k: "[", label: "Collapse / expand sidebar" },
  { k: "?", label: "Show shortcuts" }, { k: "esc", label: "Close dialog" },
];

// ---------- icons ----------
const ICON: Record<string, string> = {
  vms: '<path d="M8 1.8 13.6 5v6L8 14.2 2.4 11V5z"/><path d="M2.4 5 8 8.2 13.6 5M8 8.2v6"/>',
  tenants: '<rect x="1.8" y="4.6" width="7.4" height="7.4" rx="1"/><path d="M6.6 4.6V2.8a1 1 0 0 1 1-1h5.6a1 1 0 0 1 1 1v5.6a1 1 0 0 1-1 1H9.2"/>',
  hosts: '<path d="M2 5.2 8 2.4l6 2.8-6 2.8z"/><path d="M2 8 8 10.8 14 8M2 10.8l6 2.8 6-2.8"/>',
  templates: '<rect x="2" y="2" width="12" height="12" rx="1.6"/><path d="M2 6h12M6 6v8"/>',
  loadtest: '<path d="M2.5 11.5a5.5 5.5 0 1 1 11 0"/><path d="M8 11.5 10.6 7"/><path d="M4.2 13.5h7.6"/>',
  keys: '<rect x="1.5" y="4" width="13" height="8.5" rx="1.4"/><path d="M4 6.6h.01M6.4 6.6h.01M8.8 6.6h.01M11.2 6.6h.01M4.6 9.6h6.8"/>',
  sun: '<circle cx="8" cy="8" r="3"/><path d="M8 1.5v1.6M8 12.9v1.6M1.5 8h1.6M12.9 8h1.6M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1M3.4 12.6l1.1-1.1M11.5 4.5l1.1-1.1"/>',
  moon: '<path d="M13.2 9.6A5.6 5.6 0 0 1 6.4 2.8a5.6 5.6 0 1 0 6.8 6.8z"/>',
  rail: '<rect x="1.8" y="2.4" width="12.4" height="11.2" rx="1.6"/><path d="M6 2.4v11.2"/>',
  updown: '<path d="M5 6l3-3 3 3M5 10l3 3 3-3"/>',
  out: '<path d="M6 2.5H3.5a1 1 0 0 0-1 1v9a1 1 0 0 0 1 1H6M10.5 11l3-3-3-3M13.5 8H6"/>',
};
export const Icon = ({ k, s = 16 }: { k: string; s?: number }) => (
  <svg className="ico" width={s} height={s} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth={1.35} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" dangerouslySetInnerHTML={{ __html: ICON[k] }} />
);
export const Cube = ({ s = 18, stroke, fill }: { s?: number; stroke?: string; fill?: string }) => <span style={{ display: "inline-flex" }} dangerouslySetInnerHTML={{ __html: cubeSVG(s, { stroke, fill }) }} />;

// ---------- theme / rail ----------
export function applyPrefs() {
  document.documentElement.dataset.theme = localStorage.getItem("flint.theme") || "dark";
  document.documentElement.dataset.rail = localStorage.getItem("flint.rail") === "min" ? "min" : "";
}
const railMin = () => localStorage.getItem("flint.rail") === "min";
function setRail(min: boolean) { localStorage.setItem("flint.rail", min ? "min" : ""); applyPrefs(); }
function setTheme(t: string) {
  localStorage.setItem("flint.theme", t);
  const v = document.createElement("div"); v.className = "theme-veil"; document.body.append(v);
  requestAnimationFrame(() => v.classList.add("on"));
  window.setTimeout(() => { applyPrefs(); window.dispatchEvent(new Event("flint:theme")); requestAnimationFrame(() => { v.classList.remove("on"); window.setTimeout(() => v.remove(), 200); }); }, 160);
}
/** Re-render after a theme switch (SVG builders read tokens at draw time). */
export function useThemeVersion() {
  const [v, setV] = useState(0);
  useEffect(() => { const f = () => setV((x) => x + 1); window.addEventListener("flint:theme", f); return () => window.removeEventListener("flint:theme", f); }, []);
  return v;
}

// ---------- sidebar ----------
function Sidebar() {
  const plat = usePlatform().data;
  const cluster = plat?.mode === "cluster", admin = useIsAdmin();
  const vms = useVms().data ?? [];
  const hosts = useHosts(admin).data ?? [];
  const tpls = useTemplates().data ?? [];
  const [, force] = useState(0);
  useEffect(() => { const f = () => force((x) => x + 1); window.addEventListener("flint:recent", f); return () => window.removeEventListener("flint:recent", f); }, []);
  const nav = useRef<HTMLDivElement>(null), hov = useRef<HTMLSpanElement>(null);
  const place = (a: HTMLElement | null) => {
    const h = hov.current; if (!h) return;
    if (!a) { h.style.opacity = "0"; return; }
    h.style.opacity = "1"; h.style.transform = `translateY(${a.offsetTop}px)`; h.style.height = a.offsetHeight + "px";
  };
  const failed = vms.filter((v) => v.state === "error" || v.state === "lost").length;
  const hostIssues = hosts.filter((h) => h.state !== "ready" || h.cordoned).length;
  const starting = vms.filter((v) => v.state === "starting");
  const recent = recentVMs().map((id) => vms.find((v) => v.id === id)).filter(Boolean).slice(0, 4) as typeof vms;
  const items = [
    { to: "/vms", k: "vms", label: "VMs", key: "g f", n: vms.length, badge: failed ? <span className="bdg err" title={`${failed} failed`}>{failed}</span> : null },
    ...(cluster ? [{ to: "/tenants", k: "tenants", label: "Tenants", key: "g t", n: new Set(vms.map((v) => v.tenant).filter(Boolean)).size, badge: null }] : []),
    ...(cluster && admin ? [{ to: "/hosts", k: "hosts", label: "Hosts", key: "g h", n: hosts.length, badge: hostIssues ? <span className="bdg warn" title={`${hostIssues} not ready`}>{hostIssues}</span> : null }] : []),
    { to: "/templates", k: "templates", label: "Templates", key: "g p", n: tpls.length, badge: null },
    { to: "/loadtest", k: "loadtest", label: "Load test", key: "g l", n: null as number | null, badge: null },
  ];
  const envCol = plat?.mock ? "--info" : cluster ? "--accent" : "--ok";
  const title = cluster ? "Cluster" : "This host";
  const sub = cluster ? `${plat?.health.hosts_ready ?? 0}/${plat?.health.hosts ?? 0} hosts ready` : (plat?.health.backend_kind ?? "daemon");
  const [menu, setMenu] = useState(false);
  // The card is only there for its menu's actions: leave demo data, or sign out.
  const session = useSession().data;
  const hasMenu = !!plat?.mock || !!getToken() || !!session;
  const theme = document.documentElement.dataset.theme === "light" ? "light" : "dark";
  return (
    <nav className="rail" aria-label="Main">
      <div className="rail-top">
        <Link className="brand" to="/vms" aria-label="Flint home"><Cube s={20} /><span className="lbx">FLINT</span></Link>
        {hasMenu && (
          <button className="ws" id="wsbtn" aria-haspopup="menu" aria-expanded={menu} data-tip={`${title} · ${sub}`} onClick={() => setMenu((m) => !m)}>
            <span className="ws-mark" style={{ ["--env" as string]: `var(${envCol})` }}>{cluster ? "cp" : "fl"}</span>
            <span className="lbx ws-txt"><b>{title}</b><small><i className="env" style={{ ["--env" as string]: `var(${envCol})` }}>{plat?.mock ? "demo" : cluster ? "cluster" : "local"}</i> {sub}</small></span>
            <span className="lbx ws-chev"><Icon k="updown" s={14} /></span>
          </button>
        )}
        {menu && hasMenu && <ConnMenu onClose={() => setMenu(false)} />}
      </div>
      <div className="rail-nav" ref={nav} onMouseLeave={() => place(null)}>
        <span className="hov" ref={hov} aria-hidden="true" />
        {items.map((it) => (
          <div key={it.k} style={{ display: "contents" }}>
            <NavLink to={it.to} className={({ isActive }) => `nav${isActive ? " on" : ""}`} data-tip={`${it.label} · ${it.key}`}
              onMouseEnter={(e) => place(e.currentTarget)} onFocus={(e) => place(e.currentTarget)}>
              <span className="ic"><Icon k={it.k} /></span><span className="lbx lb">{it.label}</span>
              <span className="lbx meta">{it.badge}{it.n != null && <span className="cnt">{it.n}</span>}<span className="hk">{it.key.split(" ").map((x) => <span className="kbd" key={x}>{x}</span>)}</span></span>
            </NavLink>
            {it.k === "vms" && starting.length > 0 && (
              <Link className="sub lbx" to={`/vms/${starting[0].id}`}><span className="spin" />{starting.length} starting<span className="bar"><i /></span></Link>
            )}
          </div>
        ))}
      </div>
      {recent.length > 0 && (
        <>
          <div className="rail-h lbx">Recent</div>
          <div className="rail-rec">
            {recent.map((v) => (
              <Link key={v.id} className="rec" to={`/vms/${v.id}`} data-tip={`${v.name} · ${stateLabel(v.state)}`}>
                <StateChip state={v.state} label={false} /><span className="lbx rn">{v.name}</span>
                {v.tenant && <span className="lbx tt" style={{ color: `var(${tenantVarCss(v.tenant)})` }} title={v.tenant} />}
              </Link>
            ))}
          </div>
        </>
      )}
      <div className="rail-foot">
        <div className="ctl">
          <span className="me-acts">
            <button className="ib" aria-label="Keyboard shortcuts (?)" data-tip="Shortcuts · ?" onClick={() => window.dispatchEvent(new Event("flint:help"))}><Icon k="keys" /></button>
            <button className="ib" aria-label={`Switch to ${theme === "light" ? "dark" : "light"} theme`} data-tip={`${theme === "light" ? "Dark" : "Light"} theme`} onClick={() => setTheme(theme === "light" ? "dark" : "light")}><Icon k={theme === "light" ? "moon" : "sun"} /></button>
            <button className="ib" aria-label={`${railMin() ? "Expand" : "Collapse"} sidebar ([)`} data-tip="Sidebar · [" onClick={() => setRail(!railMin())}><Icon k="rail" /></button>
          </span>
        </div>
      </div>
    </nav>
  );
}
const tenantVarCss = (t: string) => { let h = 0; for (const ch of t) h = (h * 31 + ch.charCodeAt(0)) >>> 0; return `--tenant-${(h % 4) + 1}`; };

function ConnMenu({ onClose }: { onClose: () => void }) {
  const plat = usePlatform().data;
  const session = useSession().data;
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const out = (e: MouseEvent) => { if (!(e.target as Element).closest("#connmenu,#wsbtn")) onClose(); };
    document.addEventListener("mousedown", out); ref.current?.querySelector<HTMLButtonElement>(".mi")?.focus();
    return () => document.removeEventListener("mousedown", out);
  }, [onClose]);
  const r = document.getElementById("wsbtn")?.getBoundingClientRect();
  return (
    <div className="menu" id="connmenu" role="menu" ref={ref} style={{ left: r?.left ?? 8, top: (r?.bottom ?? 80) + 6 }} onKeyDown={(e) => e.key === "Escape" && onClose()}>
      <div className="lbl" style={{ padding: "6px 10px 4px" }}>Connected to</div>
      <div className="mi" style={{ cursor: "default" }}><span className="grow"><b>{location.host}</b><small>{plat?.mode === "cluster" ? `control plane · ${plat.health.replica ?? ""}${plat.health.leader ? " · leader" : ""}` : `daemon · ${plat?.health.backend_kind ?? ""}`}</small></span></div>
      <div className="msep" />
      {plat?.mock && <button className="mi" role="menuitem" onClick={() => { sessionStorage.removeItem("flint.mock"); location.href = location.pathname + "?live"; }}><Icon k="out" /><span className="grow">Leave demo data</span></button>}
      {session && <div className="mi" style={{ cursor: "default" }}><span className="grow">{session.identity.replace(/^[^:]*:/, "")}<small>{session.kind}{session.tenants.length ? ` · ${session.tenants.join(", ")}` : ""}</small></span></div>}
      {session && <button className="mi" role="menuitem" onClick={async () => { await signOut().catch(() => undefined); location.reload(); }}><Icon k="out" /><span className="grow">Sign out</span></button>}
      {!session && getToken() && <button className="mi" role="menuitem" onClick={() => { setToken(""); location.reload(); }}><Icon k="out" /><span className="grow">Sign out<small>forget the API token in this browser</small></span></button>}
    </div>
  );
}

// ---------- search palette ----------
function Palette({ onClose }: { onClose: () => void }) {
  const nav = useNavigate();
  const vms = useVms().data ?? [];
  const tpls = useTemplates().data ?? [];
  const admin = useIsAdmin();
  const hosts = useHosts(admin).data ?? [];
  const cluster = usePlatform().data?.mode === "cluster";
  const [q, setQ] = useState(""), [idx, setIdx] = useState(0);
  const s = q.trim().toLowerCase();
  const items = useMemo(() => {
    const out: { g: string; key: string; body: ReactNode; go: () => void }[] = [];
    vms.filter((v) => !s || [v.id, v.name, v.tenant, v.host, v.template].some((x) => x.toLowerCase().includes(s))).slice(0, 7).forEach((v) =>
      out.push({ g: "VMs", key: v.id, go: () => nav(`/vms/${v.id}`), body: <><StateChip state={v.state} label={false} /><b style={{ fontWeight: 600 }}>{v.name}</b><span className="dim">{v.shortId}</span>{v.tenant && <span>{v.tenant}</span>}<span className="dim" style={{ marginLeft: "auto" }}>{v.host || v.template}</span></> }));
    if (cluster) [...new Set(vms.map((v) => v.tenant).filter(Boolean))].filter((t) => s && t.includes(s)).forEach((t) =>
      out.push({ g: "Tenants", key: "t" + t, go: () => nav(`/tenants/${t}`), body: <><span className="tag">{t}</span><span className="dim" style={{ marginLeft: "auto" }}>{vms.filter((v) => v.tenant === t).length} VMs</span></> }));
    hosts.filter((h) => s && (h.id + h.name).toLowerCase().includes(s)).forEach((h) =>
      out.push({ g: "Hosts", key: "h" + h.id, go: () => nav(`/hosts?host=${h.id}`), body: <><b style={{ fontWeight: 600 }}>{h.name || h.id}</b><span className="dim">{h.zone}</span><span className="dim" style={{ marginLeft: "auto" }}>{h.active_vms} VMs</span></> }));
    tpls.filter((t) => s && (t.template_id + t.name).toLowerCase().includes(s)).forEach((t) =>
      out.push({ g: "Templates", key: "p" + t.template_id, go: () => nav(`/templates`), body: <><b style={{ fontWeight: 600 }}>{t.name}</b><span className="dim" style={{ marginLeft: "auto" }}>{t.status}</span></> }));
    [["New VM…", "n", () => nav("?new=1")], ["Run a load test…", "g l", () => nav("/loadtest")], ["Templates", "g p", () => nav("/templates")]].filter(([l]) => !s || String(l).toLowerCase().includes(s)).forEach(([l, k, go]) =>
      out.push({ g: "Actions", key: "a" + l, go: go as () => void, body: <><span className="sp">›</span>{l as string}<span style={{ marginLeft: "auto" }}><span className="kbd">{k as string}</span></span></> }));
    return out;
  }, [s, vms, tpls, hosts, cluster, nav]);
  const sel = Math.min(idx, items.length - 1);
  let lastG = "";
  return (
    <div className="scrim pal" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="sheet" style={{ width: 600, marginTop: 70, borderRadius: 8 }} role="dialog" aria-label="Search">
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "12px 14px", borderBottom: "1px solid var(--border)" }}>
          <Cube s={14} stroke={token("--fg-2")} fill="none" />
          <input autoFocus value={q} onChange={(e) => { setQ(e.target.value); setIdx(0); }} placeholder="VM name or id, tenant, host, template — or an action"
            style={{ flex: 1, background: "none", border: 0, outline: 0, fontSize: 14 }}
            onKeyDown={(e) => {
              if (e.key === "Escape") onClose();
              if (e.key === "ArrowDown") { setIdx(Math.min(sel + 1, items.length - 1)); e.preventDefault(); }
              if (e.key === "ArrowUp") { setIdx(Math.max(sel - 1, 0)); e.preventDefault(); }
              if (e.key === "Enter" && items[sel]) { items[sel].go(); onClose(); }
            }} />
          <span className="kbd">esc</span>
        </div>
        <div style={{ maxHeight: 380, overflow: "auto", padding: "6px 0" }}>
          {items.length === 0 && <div className="mut" style={{ padding: 14 }}>No match. Paste a VM id or name.</div>}
          {items.map((it, i) => {
            const head = it.g !== lastG ? <div className="lbl" style={{ padding: "8px 14px 4px" }}>{(lastG = it.g)}</div> : null;
            return (
              <div key={it.key}>
                {head}
                <div onMouseDown={() => { it.go(); onClose(); }} onMouseEnter={() => setIdx(i)}
                  style={{ display: "flex", gap: 10, alignItems: "center", padding: "6px 14px", cursor: "pointer", ...(i === sel ? { background: "var(--surface)", boxShadow: "inset 2px 0 var(--spark)" } : {}) }}>{it.body}</div>
              </div>
            );
          })}
        </div>
        <div className="foot" style={{ borderTop: "1px solid var(--border)", height: 28 }}><span><span className="kbd">↑↓</span> move</span><span><span className="kbd">↵</span> open</span><span className="r">searches name · id · tenant · host · template</span></div>
      </div>
    </div>
  );
}

// ---------- shell ----------
export default function Shell() {
  const [hints, setHints] = useState<KeyHint[]>([]);
  const [pal, setPal] = useState(false), [help, setHelp] = useState(false);
  const nav = useNavigate(), loc = useLocation();
  const [params, setParams] = useSearchParams();
  const gAt = useRef(0);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isTyping(e.target) || document.querySelector(".scrim")) return;
      const k = keyName(e);
      if (gAt.current && Date.now() - gAt.current < 800) {
        gAt.current = 0;
        const go: Record<string, string> = { f: "/vms", t: "/tenants", h: "/hosts", p: "/templates", l: "/loadtest" };
        if (go[e.key]) { e.preventDefault(); nav(go[e.key]); return; }
      }
      if (k === "g") { gAt.current = Date.now(); return; }
      if (k === "/" || k === "mod+k") { e.preventDefault(); setPal(true); return; }
      if (k === "?") { e.preventDefault(); setHelp(true); return; }
      if (k === "[") { e.preventDefault(); setRail(!railMin()); return; }
      if (k === "n") { e.preventDefault(); const p = new URLSearchParams(params); p.set("new", "1"); setParams(p); return; }
      dispatchPageKey(e);
    };
    const onHelp = () => setHelp(true);
    window.addEventListener("keydown", onKey); window.addEventListener("flint:help", onHelp);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("flint:help", onHelp); };
  }, [nav, params, setParams]);
  useEffect(() => { requestAnimationFrame(() => document.documentElement.classList.add("ready")); }, []);
  const closeNew = () => { const p = new URLSearchParams(params); p.delete("new"); p.delete("template"); setParams(p, { replace: true }); };
  return (
    <HintCtx.Provider value={setHints}>
      <div className="app">
        <Sidebar />
        <header className="top">
          <button className="find" id="findbtn" aria-label="Search (/)" onClick={() => setPal(true)}>
            <Cube s={13} stroke={token("--fg-3")} fill="none" /><span className="find-ph">Search VMs, tenants, hosts, actions</span><span className="kbd">/</span>
          </button>
        </header>
        <main className="main" id="main" key={loc.pathname.split("/")[1]}>
          <Outlet />
          {pal && <Palette onClose={() => setPal(false)} />}
          <Dialog open={help} onClose={() => setHelp(false)} title="Keyboard shortcuts" width={520}>
            <div className="help">
              <div className="lbl">Everywhere</div>
              {GLOBAL.map((h) => <div className="hk" key={h.k}><span>{h.label}</span><span>{h.k.split(" ").map((x) => <span className="kbd" key={x}>{x}</span>)}</span></div>)}
              {hints.length > 0 && <><div className="lbl" style={{ marginTop: 12 }}>On this page</div>{hints.map((h) => <div className="hk" key={h.k}><span>{h.label}</span><span className="kbd">{h.k}</span></div>)}</>}
            </div>
          </Dialog>
          {params.get("new") && <NewVmDrawer onClose={closeNew} template={params.get("template") ?? undefined} />}
        </main>
      </div>
    </HintCtx.Provider>
  );
}
