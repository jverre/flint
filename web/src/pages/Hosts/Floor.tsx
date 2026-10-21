// The isometric host floor: slabs = hosts, cubes = VMs, flying cubes = vm.moved events.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useEvents } from "../../api/hooks";
import type { Host, VM } from "../../api/types";
import { cubeMarkup, cubeSize, heatColor, isoMarkup, isoSlot, rowLayout, tenantVar, token, type IsoVM } from "../../kit/svg";

const C = 20, SW = 6, SD = 5, LIFT = 70, DUR = 1500, TOP = 64, SLOTS = 9;
const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
const ease = (t: number) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2);
const qpt = (a: [number, number], b: [number, number], t: number): [number, number] => {
  const cx = (a[0] + b[0]) / 2, cy = Math.min(a[1], b[1]) - LIFT;
  return [(1 - t) ** 2 * a[0] + 2 * (1 - t) * t * cx + t * t * b[0], (1 - t) ** 2 * a[1] + 2 * (1 - t) * t * cy + t * t * b[1]];
};
interface Flight { key: string; vm: string; name: string; tenant: string; from: string; to: string; fromIdx: number; t0: number }

export default function Floor({ hosts, vms, sel, onHost, themeV }: { hosts: Host[]; vms: VM[]; sel: string; onHost: (id: string) => void; themeV: number }) {
  const nav = useNavigate();
  const { events } = useEvents();
  const flights = useRef<Flight[]>([]);
  const seen = useRef<Set<string> | null>(null);
  const snap = useRef(new Map<string, { host: string; idx: number }>());
  const loop = useRef(0);
  const [, frame] = useState(0);
  const latest = useRef({ hosts, vms });
  latest.current = { hosts, vms };

  const startLoop = useCallback(() => {
    if (loop.current) return;
    const step = () => {
      const now = performance.now();
      flights.current = flights.current.filter((f) => now - f.t0 < DUR);
      frame((x) => x + 1);
      loop.current = flights.current.length ? requestAnimationFrame(step) : 0;
    };
    loop.current = requestAnimationFrame(step);
  }, []);
  useEffect(() => () => { cancelAnimationFrame(loop.current); loop.current = 0; }, []);

  // New vm.moved events become flights. The first pass only records what is already there.
  useEffect(() => {
    if (!seen.current) { seen.current = new Set(events.map((e) => e._k)); }
    else {
      const fresh = events.filter((e) => !seen.current!.has(e._k)).reverse();
      fresh.forEach((e) => seen.current!.add(e._k));
      for (const e of fresh) {
        if (e.type !== "vm.moved" || !e.vm_id || reduced()) continue;
        const from = String(e.from_host ?? ""), to = String(e.to_host ?? "");
        const known = latest.current.hosts.some((h) => h.id === from) && latest.current.hosts.some((h) => h.id === to);
        if (!known) continue;
        const s = snap.current.get(e.vm_id), v = latest.current.vms.find((x) => x.id === e.vm_id);
        flights.current.push({ key: e._k, vm: e.vm_id, name: v?.name ?? e.vm_id.slice(0, 8), tenant: v?.tenant ?? String(e.tenant ?? ""), from, to, fromIdx: s && s.host === from ? s.idx : 0, t0: performance.now() });
        startLoop();
      }
    }
    // remember where every VM that is not in flight sits, to know a flight's start slot
    const n = new Map<string, number>(), fl = new Set(flights.current.map((f) => f.vm));
    for (const v of vms) { const i = n.get(v.host) ?? 0; n.set(v.host, i + 1); if (!fl.has(v.id)) snap.current.set(v.id, { host: v.host, idx: i }); }
  });

  const flying = new Set(flights.current.map((f) => f.vm));
  const flyKey = [...flying].join(",");
  const n = hosts.length;
  const ox = 70 + SD * C * 0.866, oy = TOP;
  const W = ox + ((n - 1) * 7.5 + SW) * C * 0.866 + 16, H = oy + ((n - 1) * 7.5 + SW + SD) * C * 0.5 + 0.45 * C + 30;

  const floor = useMemo(() => {
    const ih = hosts.map((h) => {
      const out = !["ready", "draining", "drained"].includes(h.state);
      return { id: h.id, state: h.cordoned && h.state === "ready" ? "cordoned" : h.state, label: h.id + (out ? ` · ${h.state}` : "") };
    });
    const iv: IsoVM[] = vms.filter((v) => !flying.has(v.id) && v.host).map((v) => ({ id: v.id, name: v.name, host: v.host, tenant: v.tenant, state: v.state, memMiB: 100, usedMiB: 50 }));
    return isoMarkup(ih, iv, { cell: C, origin: [ox, oy], layout: rowLayout(n), highlight: { [sel]: true } });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hosts, vms, sel, themeV, flyKey, ox, oy, n]);

  // flying cubes, redrawn every animation frame
  let over = "";
  if (flights.current.length) {
    const now = performance.now(), layout = rowLayout(n), P = floor.P, s = cubeSize(1);
    const top = (hid: string, idx: number): [number, number] => {
      const hi = Math.max(0, hosts.findIndex((h) => h.id === hid)), [x, y] = isoSlot(layout[hi][0], layout[hi][1], Math.min(idx, SLOTS - 1));
      return P(x + s / 2, y + s / 2, s);
    };
    for (const f of flights.current) {
      const t = Math.min(1, (now - f.t0) / DUR);
      const dest = vms.filter((v) => v.host === f.to && v.id !== f.vm).length;
      const a = top(f.from, f.fromIdx), b = top(f.to, dest), acc = token("--accent");
      const [px, py] = qpt(a, b, ease(t));
      const Q = (x: number, y: number, z = 0): [number, number] => [px + (x - y) * C * 0.866, py + s * C + (x + y) * C * 0.5 - z * C];
      over += `<path d="M${a[0].toFixed(1)} ${a[1].toFixed(1)} Q${((a[0] + b[0]) / 2).toFixed(1)} ${(Math.min(a[1], b[1]) - LIFT).toFixed(1)} ${b[0].toFixed(1)} ${b[1].toFixed(1)}" fill="none" stroke="${acc}" stroke-dasharray="4 3" stroke-width="1.4"/>
        <circle cx="${b[0].toFixed(1)}" cy="${b[1].toFixed(1)}" r="3" fill="none" stroke="${acc}"/>
        <g>${cubeMarkup(Q, -s / 2, -s / 2, s, heatColor(0.5), { outline: token(tenantVar(f.tenant)), sel: true })}<text x="${(px + 14).toFixed(0)}" y="${(py - 12).toFixed(0)}" fill="${token("--fg-1")}" font-size="11" font-family="var(--mono)">${f.name.replace(/</g, "&lt;")}</text></g>`;
    }
  }

  return (
    <svg className="floor" viewBox={`0 0 ${W.toFixed(0)} ${H.toFixed(0)}`} preserveAspectRatio="xMidYMid meet" role="img" aria-label="Hosts as slabs, VMs as cubes"
      onClick={(e) => {
        const t = e.target as Element;
        const cube = t.closest(".cube"), slab = t.closest(".slab");
        if (cube) nav(`/vms/${cube.getAttribute("data-id")}`);
        else if (slab) onHost(slab.getAttribute("data-host") ?? "");
      }}>
      <g dangerouslySetInnerHTML={{ __html: floor.html }} />
      <g style={{ pointerEvents: "none" }} dangerouslySetInnerHTML={{ __html: over }} />
    </svg>
  );
}
