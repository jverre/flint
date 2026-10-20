import type { Host } from "../../api/types";

const LABEL: Record<string, string> = { joining: "Joining", ready: "Ready", unreachable: "Unreachable", isolated: "Isolated", down: "Down", draining: "Draining", drained: "Drained" };
const CLS: Record<string, string> = { joining: "st-starting", unreachable: "st-error", isolated: "st-error", down: "st-error", draining: "st-draining", drained: "st-cordoned" };

export function HostChip({ host }: { host: Host }) {
  const s = host.state, cls = CLS[s] ?? "";
  return (
    <span className="chip-row">
      <span className={`chip ${cls}`}><span className="dot" style={s === "ready" ? { background: "var(--ok)" } : undefined} />{LABEL[s] ?? s}</span>
      {host.cordoned && s !== "draining" && s !== "drained" && <span className="chip st-cordoned"><span className="dot" />Cordoned</span>}
    </span>
  );
}
