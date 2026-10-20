// Histogram of server start time (readyMs): SVG bars coloured on the same heat scale as the cubes.
import { useMemo, useRef } from "react";
import { heatAt, stats, type HeatRange } from "./engine";

const BINS = 24, H = 120, L = 2, R = 2, T = 16, B = 20;
const f0 = (n: number) => Math.round(n).toLocaleString("en-US");

function niceStep(max: number) { return max > 1200 ? 250 : max > 500 ? 100 : max > 200 ? 50 : max > 80 ? 20 : 10; }

export default function Hist({ vals, w, range, resetKey }: { vals: number[]; w: number; range: HeatRange; resetKey: string }) {
  // the axis only grows during a run so bars don't rescale back and forth
  const ax = useRef({ key: "", axis: 0 });
  const max = vals.length ? Math.max(...vals) : 100;
  const step = niceStep(max), want = Math.max(step * 4, Math.ceil((max * 1.1) / step) * step);
  if (ax.current.key !== resetKey) ax.current = { key: resetKey, axis: want };
  else if (want > ax.current.axis) ax.current.axis = want;
  const axis = ax.current.axis;

  const d = useMemo(() => {
    const bins = Array<number>(BINS).fill(0), bw = axis / BINS;
    vals.forEach((v) => bins[Math.min(BINS - 1, Math.floor(v / bw))]++);
    return { bins, bw, ymax: Math.max(1, ...bins) * 1.12, st: stats(vals) };
  }, [vals, axis]);

  const pw = w - L - R, ph = H - T - B, cw = pw / BINS;
  const X = (v: number) => L + Math.min(pw, (v / axis) * pw);
  const mark = (v: number, label: string, cls: string) => {
    const x = X(v), end = x > w - 70 || cls === "p50";
    return (
      <g key={cls} className={`hmark ${cls}`}>
        <line x1={x} x2={x} y1={T - 2} y2={T + ph} />
        <text x={x + (end ? -4 : 4)} y={T - 5} textAnchor={end ? "end" : "start"}>{label} {f0(v)}</text>
      </g>
    );
  };
  return (
    <svg width={w} height={H} role="img" aria-label="Start time histogram (server readyMs)" className="lt-hist">
      <line x1={L} y1={T + ph} x2={w - R} y2={T + ph} style={{ stroke: "var(--border-strong)" }} />
      {[0, 1, 2, 3, 4].map((i) => {
        const x = L + (pw * i) / 4;
        return (
          <g key={i}>
            <line x1={x} y1={T + ph} x2={x} y2={T + ph + 4} style={{ stroke: "var(--border-strong)" }} />
            <text className="hist-t" x={x} y={H - 4} textAnchor={i === 0 ? "start" : i === 4 ? "end" : "middle"}>{f0((axis * i) / 4)}{i === 4 ? " ms" : ""}</text>
          </g>
        );
      })}
      {d.bins.map((n, i) => {
        const hh = (n / d.ymax) * ph;
        return <rect key={i} className="hb" x={L + i * cw + 0.75} width={Math.max(0, cw - 1.5)} y={T + ph - hh} height={hh} style={{ fill: heatAt(range, (i + 0.5) * d.bw) }} />;
      })}
      {d.st && mark(d.st.median, "p50", "p50")}
      {d.st && mark(d.st.p99, "p99", "p99")}
    </svg>
  );
}
