// SVG builders shared by every page: cube glyph, sparkline, area chart, isometric host floor.
// They return markup strings (rendered with dangerouslySetInnerHTML inside an <svg>) and read
// colours from the theme at draw time, so call them again after a theme change.

export const token = (name: string): string =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const f1 = (n: number) => n.toFixed(1);

/** Stable categorical colour for a tenant name (tenant-1..4). */
export function tenantVar(tenant: string | null | undefined): string {
  if (!tenant) return "--fg-2";
  let h = 0;
  for (const ch of tenant) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return `--tenant-${(h % 4) + 1}`;
}

export function cubeSVG(size = 18, o: { stroke?: string; fill?: string; w?: number } = {}): string {
  const st = o.stroke ?? token("--fg-1"), fl = o.fill ?? token("--accent"), w = o.w ?? 1.3;
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="${st}" stroke-width="${w}" stroke-linejoin="round" aria-hidden="true">
 <path d="M12 2.5 21 7.5v9L12 21.5 3 16.5v-9z"/><path d="M3 7.5l9 5 9-5M12 12.5v9"/>
 <path d="M12 2.5 21 7.5 12 12.5 3 7.5z" fill="${fl}" fill-opacity=".9"/></svg>`;
}

export function sparkPoints(d: number[], w: number, h: number, max: number): string {
  if (d.length < 2) return "";
  const m = max || 1;
  return d.map((v, i) => `${f1((i / (d.length - 1)) * w)},${f1(h - (Math.min(v, m) / m) * h)}`).join(" ");
}

export interface ChartOpts {
  max?: number; unit?: string; col?: string; right?: number; limit?: number; limitLabel?: string;
  maxLabel?: string; span?: string; fmt?: (v: number) => string; marks?: { i: number; text: string }[];
}

/** Area chart with Blueprint dimension lines. Returns inner SVG markup for a W×H svg. */
export function chartMarkup(d: number[], W: number, H: number, o: ChartOpts = {}): string {
  if (d.length < 2) return "";
  const L = 4, R = o.right ?? 46, T = 10, B = 22, w = W - L - R, hh = H - T - B, max = o.max || 100;
  const col = o.col ?? "var(--h2)";
  const X = (i: number) => L + (i / (d.length - 1)) * w, Y = (v: number) => T + hh - (Math.min(v, max) / max) * hh;
  const p = d.map((v, i) => `${f1(X(i))},${f1(Y(v))}`).join(" ");
  const last = d[d.length - 1];
  const id = "hz" + Math.random().toString(36).slice(2, 7);
  const err = token("--err"), acc = token("--accent");
  return `<defs><pattern id="${id}" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="5" style="stroke:${col}" stroke-opacity=".35"/></pattern></defs>
 <line x1="${L}" y1="${T + hh}" x2="${L + w}" y2="${T + hh}" stroke="${token("--border-strong")}"/>
 ${o.limit ? `<line x1="${L}" y1="${Y(o.limit)}" x2="${L + w}" y2="${Y(o.limit)}" stroke="${err}" stroke-dasharray="3 3"/><text class="dimtxt" x="${L + 4}" y="${Y(o.limit) - 3}" style="fill:${err}">${o.limitLabel ?? "limit"}</text>` : ""}
 <polygon points="${L},${T + hh} ${p} ${L + w},${T + hh}" fill="url(#${id})"/><polyline points="${p}" fill="none" style="stroke:${col}" stroke-width="1.4"/>
 <circle cx="${X(d.length - 1)}" cy="${Y(last)}" r="3" style="fill:${col}"/>
 <path class="dimline" d="M${L + w + 10} ${T} v${hh} M${L + w + 6} ${T} h8 M${L + w + 6} ${T + hh} h8"/><text class="dimtxt" x="${L + w + 14}" y="${T + hh / 2}" transform="rotate(90 ${L + w + 14} ${T + hh / 2})" text-anchor="middle">${o.maxLabel ?? max + " " + (o.unit ?? "%")}</text>
 <path class="dimline" d="M${L} ${T + hh + 12} h${w} M${L} ${T + hh + 8} v8 M${L + w} ${T + hh + 8} v8"/><rect x="${L + w / 2 - 22}" y="${T + hh + 6}" width="44" height="12" fill="${token("--bg-app")}"/><text class="dimtxt" x="${L + w / 2}" y="${T + hh + 16}" text-anchor="middle">${o.span ?? "60 s"}</text>
 ${(o.marks ?? []).map((m) => `<line x1="${X(m.i)}" y1="${T}" x2="${X(m.i)}" y2="${T + hh}" stroke="${acc}" stroke-dasharray="2 2"/><text class="dimtxt" x="${X(m.i) + 4}" y="${T + 9}" style="fill:${acc}">${m.text}</text>`).join("")}
 <line x1="${X(d.length - 1)}" y1="${Y(last)}" x2="${X(d.length - 1) - 16}" y2="${T + 2}" class="dimline"/><text class="dimtxt" x="${X(d.length - 1) - 18}" y="${T + 1}" text-anchor="end" style="fill:${token("--fg-1")}">${o.fmt ? o.fmt(last) : last.toFixed(1) + " " + (o.unit ?? "%")}</text>`;
}

// ---------- isometric floor ----------
export interface IsoHost { id: string; state?: string; label?: string }
export interface IsoVM { id: string; name: string; host: string | null; tenant?: string | null; state: string; vcpu?: number; memMiB?: number; usedMiB?: number }
export interface IsoOpts {
  cell?: number; origin?: [number, number]; layout?: [number, number][]; sel?: string | null;
  highlight?: Record<string, boolean>; labels?: boolean;
}
export type Proj = (x: number, y: number, z?: number) => [number, number];

/** Slot (in floor units) of the i-th VM on a host whose slab origin is (bx,by). */
export const isoSlot = (bx: number, by: number, i: number): [number, number] => [bx + 0.9 + (i % 3) * 1.7, by + 1 + Math.floor(i / 3) * 1.8];
export const cubeSize = (vcpu = 1) => (vcpu >= 4 ? 1.35 : vcpu >= 2 ? 1.05 : 0.8);

export function isoProjection(o: IsoOpts): Proj {
  const C = o.cell ?? 22, ox = o.origin?.[0] ?? 60, oy = o.origin?.[1] ?? 140;
  return (x, y, z = 0) => [ox + (x - y) * C * 0.866, oy + (x + y) * C * 0.5 - z * C];
}

const pt = (a: [number, number][]) => a.map((p) => p.map(f1).join(",")).join(" ");

export function heatColor(frac: number): string {
  return frac > 0.9 ? token("--err") : frac > 0.66 ? token("--heat-4") : frac > 0.4 ? token("--heat-3") : frac > 0.2 ? token("--heat-2") : token("--heat-1");
}

/** One isometric cube's polygons at floor position (x,y) with edge s. */
export function cubeMarkup(P: Proj, x: number, y: number, s: number, col: string, o: { outline?: string; sel?: boolean; idle?: boolean; paused?: boolean; extra?: string } = {}): string {
  const A = P(x, y), B = P(x + s, y), Cc = P(x + s, y + s), D = P(x, y + s);
  const At = P(x, y, s), Bt = P(x + s, y, s), Ct = P(x + s, y + s, s), Dt = P(x, y + s, s);
  const edge = token("--cube-edge"), idle = !!o.idle;
  return `${o.outline ? `<polygon points="${pt([A, B, Cc, D])}" fill="none" stroke="${o.outline}" stroke-width="1.2" transform="translate(0,2)"/>` : ""}
  <polygon points="${pt([D, Cc, Ct, Dt])}" fill="${col}" fill-opacity="${idle ? 1 : 0.78}" stroke="${edge}" stroke-width=".6"/>
  <polygon points="${pt([B, Cc, Ct, Bt])}" fill="${col}" fill-opacity="${idle ? 0.8 : 0.58}" stroke="${edge}" stroke-width=".6"/>
  <polygon points="${pt([At, Bt, Ct, Dt])}" fill="${col}" stroke="${o.sel ? token("--accent") : edge}" stroke-width="${o.sel ? 2 : 0.6}"/>
  ${o.paused ? `<text x="${Ct[0] - 3}" y="${(At[1] + Ct[1]) / 2 + 4}" fill="${edge}" font-size="10" font-weight="700">‖</text>` : ""}${o.extra ?? ""}`;
}

/** Full floor: host slabs + VM cubes. Click handling: cubes carry data-id, slabs data-host. */
export function isoMarkup(hosts: IsoHost[], vms: IsoVM[], o: IsoOpts = {}): { html: string; P: Proj } {
  const P = isoProjection(o);
  let g = "";
  const placed: { v: IsoVM; x: number; y: number }[] = [];
  hosts.forEach((h, hi) => {
    const bx = o.layout?.[hi]?.[0] ?? hi * 7.5, by = o.layout?.[hi]?.[1] ?? 0, sw = 6, sd = 5, th = 0.45;
    const cord = ["cordoned", "draining", "drained"].includes(h.state ?? "");
    const top = [P(bx, by), P(bx + sw, by), P(bx + sw, by + sd), P(bx, by + sd)];
    const fr = [P(bx, by + sd), P(bx + sw, by + sd), P(bx + sw, by + sd, -th), P(bx, by + sd, -th)];
    const rt = [P(bx + sw, by), P(bx + sw, by + sd), P(bx + sw, by + sd, -th), P(bx + sw, by, -th)];
    g += `<g class="slab" data-host="${h.id}"><polygon points="${pt(fr)}" fill="${token("--slab-side")}" stroke="${token("--border-default")}"/><polygon points="${pt(rt)}" fill="${token("--slab-side-2")}" stroke="${token("--border-default")}"/>
  <polygon points="${pt(top)}" fill="${cord ? "url(#cordon)" : token("--slab-top")}" stroke="${o.highlight?.[h.id] ? token("--accent") : cord ? token("--warn") : token("--border-strong")}" ${cord ? 'stroke-dasharray="5 3"' : ""}/>`;
    for (let i = 1; i < sw; i++) g += `<line x1="${f1(P(bx + i, by)[0])}" y1="${f1(P(bx + i, by)[1])}" x2="${f1(P(bx + i, by + sd)[0])}" y2="${f1(P(bx + i, by + sd)[1])}" stroke="${token("--slab-grid")}"/>`;
    for (let j = 1; j < sd; j++) g += `<line x1="${f1(P(bx, by + j)[0])}" y1="${f1(P(bx, by + j)[1])}" x2="${f1(P(bx + sw, by + j)[0])}" y2="${f1(P(bx + sw, by + j)[1])}" stroke="${token("--slab-grid")}"/>`;
    if (o.labels !== false) {
      const lp = P(bx, by + sd, -th);
      g += `<text x="${f1(lp[0] - 4)}" y="${f1(lp[1] + 14)}" text-anchor="end" fill="${cord ? token("--warn") : token("--fg-2")}" font-size="10.5" font-family="var(--mono)">${h.label ?? h.id}${cord ? " · " + h.state : ""}</text>`;
    }
    g += `</g>`;
    vms.filter((v) => v.host === h.id).slice(0, 9).forEach((v, i) => { const [x, y] = isoSlot(bx, by, i); placed.push({ v, x, y }); });
  });
  placed.sort((a, b) => a.x + a.y - (b.x + b.y)).forEach(({ v, x, y }) => {
    const s = cubeSize(v.vcpu), frac = v.memMiB ? (v.usedMiB ?? 0) / v.memMiB : 0;
    const idle = v.state !== "running";
    const col = v.state === "error" ? token("--err") : idle ? token("--cube-idle") : heatColor(frac);
    g += `<g class="cube" data-id="${v.id}" style="cursor:pointer;${v.state === "starting" ? "animation:blink .6s infinite alternate" : ""}">${cubeMarkup(P, x, y, s, col, { outline: token(tenantVar(v.tenant)), sel: o.sel === v.id, idle, paused: v.state === "paused" })}</g>`;
  });
  const html = `<defs><pattern id="cordon" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="8" height="8" fill="${token("--slab-top")}"/><line x1="0" y1="0" x2="0" y2="8" stroke="${token("--warn")}" stroke-opacity=".25" stroke-width="3"/></pattern></defs>` + g;
  return { html, P };
}

/** Default row layout used by every floor: hosts along the diagonal, 7.5 units apart. */
export const rowLayout = (n: number): [number, number][] => Array.from({ length: n }, (_, i) => [i * 7.5, 0]);
