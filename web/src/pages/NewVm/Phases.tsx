// Boot phase breakdown from the create response's `timings` (names are whatever the server returned).
const SHADE = ["--heat-1", "--heat-2", "--heat-3", "--heat-4", "--info"];
const LABEL: Record<string, string> = { placing: "Placing", jail: "Isolating", netns: "Networking", restore: "Restoring snapshot", agent_connect: "Agent connect" };
export const phaseLabel = (k: string) => LABEL[k] ?? LABEL[k.replace(/_ms$/, "")] ?? (k.replace(/_ms$/, "").replace(/[_-]+/g, " ").replace(/^./, (c) => c.toUpperCase()));
const fmt = (n: number) => (n >= 100 ? n.toFixed(0) : n.toFixed(1));

export function Phases({ timings, readyMs }: { timings: Record<string, number>; readyMs: number | null }) {
  const rows = Object.entries(timings).filter(([, v]) => typeof v === "number");
  if (!rows.length) return <p className="nv-note">The server returned no phase timings for this VM.</p>;
  const sum = rows.reduce((a, [, v]) => a + v, 0) || 1;
  const col = (i: number) => `var(${SHADE[i % SHADE.length]})`;
  return (
    <div className="nv-phases">
      <div className="nv-stack" role="img" aria-label={`Boot phases: ${rows.map(([k, v]) => `${phaseLabel(k)} ${fmt(v)} ms`).join(", ")}`}>
        {rows.map(([k, v], i) => <span key={k} title={`${phaseLabel(k)} · ${fmt(v)} ms`} style={{ flex: `${Math.max(v, sum * 0.01)} 1 0`, background: col(i) }} />)}
      </div>
      <div className="nv-prows">
        {rows.map(([k, v], i) => (
          <div className="nv-prow" key={k}>
            <span className="g" style={{ background: col(i) }} />
            <span>{phaseLabel(k)} <small>{k}</small></span>
            <span className="ms">{fmt(v)}<i> ms</i></span>
            <span className="pc">{Math.round((v / sum) * 100)} %</span>
          </div>
        ))}
        <div className="nv-prow tot"><span /><span>Ready (server total)</span><span className="ms">{readyMs == null ? fmt(sum) : fmt(readyMs)}<i> ms</i></span><span /></div>
      </div>
    </div>
  );
}
