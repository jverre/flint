// Isometric floor for the load test. Same look as the shared floor (kit/svg.ts isoMarkup: slabs,
// projection, tokens) but with a per-slab slot grid of Sz×Sz cells so 100 cubes fit, and cubes as
// real SVG elements so each one can drop in, flash and settle on the heat scale.
import { useMemo, useRef, type CSSProperties, type ReactElement } from "react";
import { isoProjection } from "../../kit/svg";
import { heatAt, type HeatRange, type LtResult } from "./engine";

export interface FloorHost { id: string; label: string; sub?: string; state?: string }
export interface FloorVM { id: string; host: string }

const TH = 0.45, Z0 = 2.2, CF = 0.66, PGAP = 1.6, MAX_SZ = 12;

/** Cells of a slab in fill order: back corner first. */
const cellOrder = (sz: number): [number, number][] => {
  const o: [number, number][] = [];
  for (let y = 0; y < sz; y++) for (let x = 0; x < sz; x++) o.push([x, y]);
  return o.sort((a, b) => a[0] + a[1] - (b[0] + b[1]) || a[0] - b[0]);
};

export function slabSize(hosts: FloorHost[], existing: FloorVM[], lt: LtResult[], expect: number): number {
  const per = Math.ceil(expect / Math.max(1, hosts.length));
  let m = 0;
  for (const h of hosts) {
    const ex = existing.filter((v) => v.host === h.id).length;
    const n = lt.filter((r) => r.ok && r.host === h.id).length;
    m = Math.max(m, ex + Math.max(n, per));
  }
  return Math.min(MAX_SZ, Math.max(3, Math.ceil(Math.sqrt(m))));
}

interface Props {
  w: number; h: number; hosts: FloorHost[]; existing: FloorVM[]; lt: LtResult[];
  range: HeatRange; gone: Set<string>; expect: number; resetKey: string;
}

export default function Floor({ w, h, hosts, existing, lt, range, gone, expect, resetKey }: Props) {
  // slab size only grows within a run so cubes never re-flow while it is going
  const hw = useRef({ key: "", sz: 3 });
  const want = slabSize(hosts, existing, lt, expect);
  if (hw.current.key !== resetKey) hw.current = { key: resetKey, sz: want };
  else if (want > hw.current.sz) hw.current.sz = want;
  const Sz = hw.current.sz;

  const geo = useMemo(() => {
    const n = Math.max(1, hosts.length), cols = n === 1 ? 1 : n <= 4 ? 2 : 3, rows = Math.ceil(n / cols);
    const X = cols * Sz + (cols - 1) * PGAP, Y = rows * Sz + (rows - 1) * PGAP;
    const span = (X + Y) * 0.5 + TH + Z0 + CF;
    const C = Math.max(6, Math.min(46, (w - 24) / ((X + Y) * 0.866), (h - 24) / span));
    const top = Math.max(6, (h - span * C) / 2), oy = top + (Z0 + CF) * C;
    const ox = w / 2 - ((X - Y) / 2) * 0.866 * C;
    return { cols, rows, C, P: isoProjection({ cell: C, origin: [ox, oy] }) };
  }, [hosts.length, Sz, w, h]);
  const { cols, C, P } = geo;
  const pt = (a: [number, number][]) => a.map((p) => p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" ");

  const order = useMemo(() => cellOrder(Sz), [Sz]);
  const cap = Sz * Sz;

  // slot assignment: existing VMs first (stable by id), then load-test VMs in arrival order
  const cubes: { key: string; k: number; slot: number; kind: "exist" | "lt"; r?: LtResult; id: string }[] = [];
  hosts.forEach((hst, k) => {
    const mine = lt.filter((r) => r.ok && r.host === hst.id);
    const ex = existing.filter((v) => v.host === hst.id).map((v) => v.id).sort();
    const shown = Math.min(ex.length, Math.max(0, cap - mine.length));
    ex.slice(0, shown).forEach((id, i) => cubes.push({ key: id, k, slot: i, kind: "exist", id }));
    mine.slice(0, cap - shown).forEach((r, i) => cubes.push({ key: r.id, k, slot: shown + i, kind: "lt", r, id: r.id }));
  });
  const place = (c: { k: number; slot: number }) => {
    const [cx, cy] = order[c.slot];
    const bx = (c.k % cols) * (Sz + PGAP), by = Math.floor(c.k / cols) * (Sz + PGAP);
    return { gx: bx + cx + 0.5 - CF / 2, gy: by + cy + 0.5 - CF / 2 };
  };
  const sorted = cubes.map((c) => ({ c, ...place(c) })).sort((a, b) => a.gx + a.gy - (b.gx + b.gy) || a.gx - b.gx);
  // keep DOM order stable: React moves nodes when order changes, which would restart animations,
  // and a slot never changes once taken, so the depth order of existing cubes is stable.

  const rp = [[0, -0.5], [0.866, 0], [0, 0.5], [-0.866, 0]].map((p) => (p[0] * C).toFixed(1) + "," + (p[1] * C).toFixed(1)).join(" ");
  const counts = (id: string) => existing.filter((v) => v.host === id).length + lt.filter((r) => r.ok && r.host === id).length;

  return (
    <svg width={w} height={h} aria-hidden="true" className="lt-floor-svg" style={{ ["--drop" as string]: `${(Z0 * C).toFixed(1)}px`, ["--rise" as string]: `${(C * 1.6).toFixed(1)}px` }}>
      {hosts.map((hst, k) => {
        const bx = (k % cols) * (Sz + PGAP), by = Math.floor(k / cols) * (Sz + PGAP);
        const top = [P(bx, by), P(bx + Sz, by), P(bx + Sz, by + Sz), P(bx, by + Sz)] as [number, number][];
        const fr = [P(bx, by + Sz), P(bx + Sz, by + Sz), P(bx + Sz, by + Sz, -TH), P(bx, by + Sz, -TH)] as [number, number][];
        const rt = [P(bx + Sz, by), P(bx + Sz, by + Sz), P(bx + Sz, by + Sz, -TH), P(bx + Sz, by, -TH)] as [number, number][];
        const left = hosts.length > 1 && k % cols === 0;
        const lp = k === 0 && hosts.length > 1 ? P(bx, by + Sz * 0.5) : left ? P(bx, by + Sz) : P(bx + Sz, by);
        const lx = lp[0] + (left ? -9 : 9), ly = lp[1] - 1 - (k === 0 && hosts.length > 1 ? CF * C + 6 : 0);
        const grid: ReactElement[] = [];
        for (let i = 1; i < Sz; i++) {
          const a = P(bx + i, by), b = P(bx + i, by + Sz), c = P(bx, by + i), d = P(bx + Sz, by + i);
          grid.push(<line key={"x" + i} x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]} style={{ stroke: "var(--slab-grid)" }} />);
          grid.push(<line key={"y" + i} x1={c[0]} y1={c[1]} x2={d[0]} y2={d[1]} style={{ stroke: "var(--slab-grid)" }} />);
        }
        const dim = hst.state && !["ready", "local"].includes(hst.state);
        return (
          <g key={hst.id} className="slab" data-host={hst.id} style={dim ? { opacity: 0.55 } : undefined}>
            <polygon points={pt(fr)} style={{ fill: "var(--slab-side)", stroke: "var(--border-default)" }} />
            <polygon points={pt(rt)} style={{ fill: "var(--slab-side-2)", stroke: "var(--border-default)" }} />
            <polygon points={pt(top)} style={{ fill: "var(--slab-top)", stroke: "var(--border-strong)" }} />
            {grid}
            <polygon points={pt(top)} style={{ fill: "var(--accent)", opacity: 0.07 }} />
            <polygon points={pt(top)} style={{ fill: "none", stroke: "var(--accent)", strokeWidth: 7, strokeLinejoin: "round", opacity: 0.22 }} />
            <polygon points={pt(top)} style={{ fill: "none", stroke: "var(--accent)", strokeWidth: 1.6, strokeLinejoin: "round" }} />
            <text className="lbl-t on" x={lx} y={ly} textAnchor={left ? "end" : "start"}>
              {hst.label}
              <tspan className="sub" x={lx} dy={13}>{hst.sub ? `${hst.sub} · ` : ""}{counts(hst.id)} VMs</tspan>
            </text>
          </g>
        );
      })}
      {sorted.map(({ c, gx, gy }) => {
        const s = CF, x = gx, y = gy;
        const D = P(x, y + s), Cc = P(x + s, y + s), B = P(x + s, y), Dt = P(x, y + s, s), Ct = P(x + s, y + s, s), Bt = P(x + s, y, s), At = P(x, y, s);
        const pc = P(x + s / 2, y + s / 2);
        const isGone = gone.has(c.id);
        const col = c.r ? heatAt(range, c.r.readyMs ?? c.r.clientMs) : undefined;
        return (
          <g key={c.key} className={`lt-cube ${c.kind === "lt" ? "lt" : "exist"}${isGone ? " gone" : ""}`} data-id={c.id} data-kind={c.kind}
            style={col ? ({ ["--c" as string]: col } as CSSProperties) : undefined}>
            {c.kind === "lt" && <g transform={`translate(${pc[0].toFixed(1)},${pc[1].toFixed(1)})`}><polygon className="ring" points={rp} /></g>}
            <g className="body">
              <polygon className="L" points={pt([D, Cc, Ct, Dt])} />
              <polygon className="Rr" points={pt([B, Cc, Ct, Bt])} />
              <polygon className="Tp" points={pt([At, Bt, Ct, Dt])} />
              {c.kind === "lt" && <g className="flash"><polygon points={pt([D, Cc, Ct, Dt])} /><polygon points={pt([B, Cc, Ct, Bt])} /><polygon points={pt([At, Bt, Ct, Dt])} /></g>}
            </g>
          </g>
        );
      })}
    </svg>
  );
}
