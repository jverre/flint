import { memo, useEffect } from "react";
import { Link } from "react-router-dom";
import { Spark } from "../../components/ui";
import { useMetrics } from "../../api/hooks";
import type { VM } from "../../api/types";
import { fmtMiB, fmtMs, fmtSince } from "../../kit/format";

export const METRICS_CAP = 40;
const MIB = 1048576;

const subText = (v: VM) =>
  v.state === "starting" ? <small className="sp">starting…</small>
  : v.state === "error" ? <small className="errc">failed</small>
  : v.state === "lost" ? <small className="wrn">lost</small>
  : v.state === "paused" ? <small>paused</small>
  : v.state === "unknown" ? <small className="dim" title={`state: ${v.raw.cluster_state || v.raw.state}`}>unknown state</small>
  : <small>{v.template}</small>;

export interface RowProps {
  vm: VM; index: number; cluster: boolean; cursor: boolean; checked: boolean; selMode: boolean; fresh: boolean;
  maxRss: number; onRss: (bytes: number) => void;
  onOpen: (id: string) => void; onCursor: (id: string) => void; onToggle: (id: string, shift: boolean) => void;
}

/** One row = one metrics query (running VMs among the first METRICS_CAP only). */
export const VmRow = memo(function VmRow(p: RowProps) {
  const { vm, onRss } = p;
  const live = vm.state === "running" && p.index < METRICS_CAP;
  const m = useMetrics(vm.id, live, 4000);
  const samples = live ? m.data : undefined;
  const last = samples?.[samples.length - 1];
  const rss = last?.rss_bytes ?? 0;
  const cpu = last?.cpu_percent ?? 0;
  useEffect(() => { if (rss) onRss(rss); }, [rss, onRss]);
  const frac = rss ? Math.min(1, rss / Math.max(p.maxRss, rss)) : 0;
  const cls = [p.cursor ? "sel" : "", p.checked ? "chk" : "", p.fresh ? "fresh" : ""].filter(Boolean).join(" ");

  return (
    <tr data-id={vm.id} className={cls}
      onClick={(e) => {
        if ((e.target as Element).closest("a")) return;
        p.onCursor(vm.id);
        if (p.selMode) p.onToggle(vm.id, e.shiftKey); else p.onOpen(vm.id);
      }}>
      {p.selMode && (
        <td className="vms-cb">
          <button className="cb" role="checkbox" aria-checked={p.checked} aria-label={`Select ${vm.name}`}
            onClick={(e) => { e.stopPropagation(); p.onCursor(vm.id); p.onToggle(vm.id, e.shiftKey); }} />
        </td>
      )}
      <td>
        <div className="nm">
          <span className={`chip st-${vm.state}`} title={vm.state}><span className="dot" /></span>
          <Link to={`/vms/${vm.id}`} onClick={(e) => { if (p.selMode) e.preventDefault(); }}><b>{vm.name}</b></Link>
          {subText(vm)}
        </div>
      </td>
      {p.cluster && <td className={vm.tenant ? "" : "dim"}>{vm.tenant || "—"}</td>}
      {p.cluster && <td className="mut">{vm.host || "—"}</td>}
      <td>
        {live && samples && samples.length > 1 ? (
          <div className="row">
            <Spark data={samples.map((s) => s.cpu_percent)} w={36} h={14} max={100} col={cpu > 80 ? "var(--warn)" : "var(--h2)"} />
            <span className={cpu > 80 ? "wrn" : "mut"}>{Math.round(cpu)} %</span>
          </div>
        ) : <span className="dim">—</span>}
      </td>
      <td>
        {rss ? (
          <div className="row">
            <div className="meter" style={{ width: 32 }}><i style={{ width: `${frac * 100}%` }} /></div>
            <span>{fmtMiB(rss / MIB)}</span>
          </div>
        ) : <span className="dim">—</span>}
      </td>
      <td className="num mut">{fmtSince(vm.createdAt)}</td>
      <td className="num mut">{fmtMs(vm.readyMs)}</td>
    </tr>
  );
});
