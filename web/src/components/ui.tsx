// Shared building blocks. Same markup/classes as the design kit (styles/kit.css), so the design
// system in flint-design/flows/DESIGN-SYSTEM.md applies 1:1.
import * as RD from "@radix-ui/react-dialog";
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { sparkPoints } from "../kit/svg";
import { stateLabel } from "../kit/format";

// ---------- section header (Blueprint marker) ----------
export function Sec({ l, title, meta }: { l: string; title: ReactNode; meta?: ReactNode }) {
  return (
    <div className="sec">
      <div className="m" aria-hidden="true"><span>{l}</span></div>
      <h2>{title}</h2>
      {meta != null && <span className="meta">{meta}</span>}
    </div>
  );
}

// ---------- sparkline ----------
export function Spark({ data, w = 80, h = 18, max, col = "var(--h2)" }: { data: number[]; w?: number; h?: number; max?: number; col?: string }) {
  const m = max ?? Math.max(1, ...data) * 1.15;
  return (
    <svg width={w} height={h} style={{ display: "block" }} aria-hidden="true">
      <polyline points={sparkPoints(data, w, h, m)} fill="none" style={{ stroke: col }} strokeWidth={1.2} />
    </svg>
  );
}

// ---------- KPI strip ----------
export interface Kpi { label: string; value: ReactNode; unit?: ReactNode; spark?: number[]; max?: number; col?: string }
export function Kpis({ items }: { items: Kpi[] }) {
  return (
    <div className="kpis">
      {items.map((k) => (
        <div className="kpi" key={k.label}>
          <div className="lbl">{k.label}</div>
          <div className="v">{k.value}{k.unit != null && <i> {k.unit}</i>}</div>
          {k.spark && k.spark.length > 1 && <Spark data={k.spark} w={100} h={18} max={k.max} col={k.col} />}
        </div>
      ))}
    </div>
  );
}

// ---------- status chip ----------
export function StateChip({ state, label = true }: { state: string; label?: boolean }) {
  const cls = state === "creating" ? "starting" : state;
  return (
    <span className={`chip st-${cls}`} title={stateLabel(state)}>
      <span className="dot" />{label && stateLabel(state)}
    </span>
  );
}

// ---------- activity feed ----------
export interface FeedItem { key: string; time: string; color: string; body: ReactNode; fresh?: boolean }
export function Feed({ items, max = 30 }: { items: FeedItem[]; max?: number }) {
  return (
    <div className="feed" aria-live="off">
      {items.slice(0, max).map((it) => (
        <div className={`ev${it.fresh ? " new" : ""}`} key={it.key}>
          <span className="t">{it.time}</span>
          <span className="g" style={{ background: it.color }} />
          <span>{it.body}</span>
        </div>
      ))}
    </div>
  );
}

// ---------- tabs ----------
export interface TabDef { id: string; label: ReactNode; count?: ReactNode }
export function Tabs({ tabs, active, onChange }: { tabs: TabDef[]; active: string; onChange: (id: string) => void }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <button key={t.id} role="tab" aria-selected={t.id === active} className={`tab${t.id === active ? " on" : ""}`} onClick={() => onChange(t.id)}>
          {t.label}{t.count != null && <i>{t.count}</i>}
        </button>
      ))}
    </div>
  );
}

// ---------- dialog (modal or right drawer) ----------
export interface DialogAction { label: ReactNode; kind?: "pri" | "danger" | "ghost" | ""; onClick?: () => void | boolean | Promise<void | boolean>; disabled?: boolean; id?: string }
export function Dialog({ open, onClose, title, sub, children, actions, foot, side, width }: {
  open: boolean; onClose: () => void; title: ReactNode; sub?: ReactNode; children?: ReactNode;
  actions?: DialogAction[]; foot?: ReactNode; side?: boolean; width?: number;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <RD.Root open={open} onOpenChange={(o) => !o && onClose()}>
      <RD.Portal container={document.getElementById("main") ?? undefined}>
        <RD.Overlay className={`scrim${side ? " right" : ""}`}>
          <RD.Content className={`dlg${side ? " drawer" : ""}`} style={width ? { width } : undefined} aria-describedby={undefined}>
            <header className="dlg-h">
              <div><RD.Title asChild><h2 id="dlg-t">{title}</h2></RD.Title>{sub && <p className="dlg-sub">{sub}</p>}</div>
              <RD.Close className="x" aria-label="Close">×</RD.Close>
            </header>
            <div className="dlg-b">{children}</div>
            {actions && (
              <footer className="dlg-f">
                {foot}<span className="sp-r" />
                {actions.map((a, i) => (
                  <button key={i} id={a.id} className={`btn ${a.kind ?? ""}`} disabled={a.disabled || busy}
                    onClick={async () => {
                      if (!a.onClick) return onClose();
                      setBusy(true);
                      try { const r = await a.onClick(); if (r !== false) onClose(); } finally { setBusy(false); }
                    }}>{a.label}</button>
                ))}
              </footer>
            )}
          </RD.Content>
        </RD.Overlay>
      </RD.Portal>
    </RD.Root>
  );
}

// ---------- toasts ----------
interface ToastMsg { id: number; body: ReactNode; action?: string; onAction?: () => void; kind?: "err" }
const ToastCtx = createContext<(body: ReactNode, o?: { action?: string; onAction?: () => void; timeout?: number; kind?: "err" }) => void>(() => {});
export const useToast = () => useContext(ToastCtx);
export function ToastProvider({ children }: { children: ReactNode }) {
  const [t, setT] = useState<ToastMsg | null>(null);
  const timer = useRef<number>(0);
  const show = useCallback((body: ReactNode, o: { action?: string; onAction?: () => void; timeout?: number; kind?: "err" } = {}) => {
    window.clearTimeout(timer.current);
    setT({ id: Date.now(), body, ...o });
    timer.current = window.setTimeout(() => setT(null), o.timeout ?? (o.kind === "err" ? 9000 : 6000));
  }, []);
  return (
    <ToastCtx.Provider value={show}>
      {children}
      <div aria-live="polite" className="sr">{t ? (typeof t.body === "string" ? t.body : "") : ""}</div>
      {t && (
        <div className={`toast kit${t.kind === "err" ? " err" : ""}`} role="status" key={t.id}>
          <span>{t.body}</span>
          {t.action && <button className="btn sm" onClick={() => { setT(null); t.onAction?.(); }}>{t.action}</button>}
          <button className="x" aria-label="Dismiss" onClick={() => setT(null)}>×</button>
        </div>
      )}
    </ToastCtx.Provider>
  );
}

// ---------- empty / error / loading ----------
export function Empty({ title, children, action }: { title: ReactNode; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <div className="empty-t">{title}</div>
      {children && <div className="empty-b">{children}</div>}
      {action}
    </div>
  );
}
export function SkeletonRows({ rows = 8, cols = 6 }: { rows?: number; cols?: number }) {
  return (
    <>{Array.from({ length: rows }, (_, r) => (
      <tr key={r} className="skel">{Array.from({ length: cols }, (_, c) => <td key={c}><span className="sk" style={{ width: `${40 + ((r * 7 + c * 13) % 50)}%` }} /></td>)}</tr>
    ))}</>
  );
}

/** Re-render every `ms` (for "3s ago" style labels). */
export function useTick(ms = 1000) {
  const [, set] = useState(0);
  useEffect(() => { const t = window.setInterval(() => set((x) => x + 1), ms); return () => window.clearInterval(t); }, [ms]);
}
