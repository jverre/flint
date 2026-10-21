// Host events: live (useEvents) merged with the history fetched once from GET /v1/events.
import { useMemo } from "react";
import { useEventHistory } from "../../api/hosts";
import { useEvents } from "../../api/hooks";
import type { FlintEvent, VM } from "../../api/types";
import { Feed, type FeedItem } from "../../components/ui";
import { fmtClock } from "../../kit/format";

export const isHostEvent = (e: FlintEvent) => e.type.startsWith("host.") || e.type === "vm.moved" || e.type === "vm.lost";
const COLOR: Record<string, string> = { "host.ready": "var(--ok)", "host.draining": "var(--warn)", "host.drained": "var(--ok)", "host.down": "var(--err)", "host.unreachable": "var(--err)", "host.isolated": "var(--err)", "vm.lost": "var(--err)", "vm.moved": "var(--spark)" };

export function HostFeed({ vms }: { vms: VM[] }) {
  const live = useEvents().events, hist = useEventHistory();
  const items = useMemo<FeedItem[]>(() => {
    const name = (id?: string) => (id ? vms.find((v) => v.id === id)?.name ?? id.slice(0, 8) : "");
    const seen = new Set<string>(), all: (FlintEvent & { _k?: string })[] = [];
    for (const e of [...live, ...(hist.data ?? [])]) {
      if (!isHostEvent(e)) continue;
      const k = `${e.type}|${Math.round(e.ts * 10)}|${e.vm_id ?? ""}|${e.host_id ?? ""}`;
      if (seen.has(k)) continue;
      seen.add(k); all.push(e);
    }
    all.sort((a, b) => b.ts - a.ts);
    return all.slice(0, 40).map((e, i) => {
      const host = <b>{String(e.host_id ?? "")}</b>;
      let body;
      if (e.type === "vm.moved") body = <><b>{name(e.vm_id)}</b> moved {String(e.from_host ?? "?")} → {String(e.to_host ?? "?")}{e.reason ? ` (${String(e.reason)})` : ""}</>;
      else if (e.type === "vm.lost") body = <><b>{name(e.vm_id)}</b> lost{e.host_id ? <> on {String(e.host_id)}</> : null}</>;
      else body = <>{host} {e.type.slice(5).replace(/_/g, " ")}</>;
      return { key: e._k ?? `h${i}${e.ts}`, time: fmtClock(e.ts), color: COLOR[e.type] ?? "var(--dim)", body, fresh: !!e._k && i === 0 };
    });
  }, [live, hist.data, vms]);
  if (!items.length) return <p className="mut" style={{ font: "12px var(--sans)" }}>{hist.isLoading ? "Loading…" : "No host events yet."}</p>;
  return <Feed items={items} max={14} />;
}
