import { useEffect, useMemo, useRef, useState } from "react";
import { openSocket } from "../../api/client";
import { fmtClock } from "../../kit/format";
import { NeedsRunning, type TabProps } from "./common";

interface Line { n: number; ts: string; msg: string }
const RE = /^\[(\d{2}:\d{2}:\d{2})\]\s?(.*)$/s;
const MAX = 3000;

export default function Logs({ vm, running, onResume, resuming }: TabProps) {
  const [lines, setLines] = useState<Line[]>([]);
  const [conn, setConn] = useState<"connecting" | "live" | "closed">("connecting");
  const [nonce, setNonce] = useState(0);
  const [frozen, setFrozen] = useState<number | null>(null); // number of lines shown while paused
  const [q, setQ] = useState("");
  const el = useRef<HTMLDivElement>(null), seq = useRef(0);

  useEffect(() => {
    if (!running) return;
    let dead = false;
    setLines([]); seq.current = 0; setConn("connecting");
    const s = openSocket(`/vms/${vm.id}/logs`);
    let buf: Line[] = [], timer = 0;
    const flush = () => { timer = 0; const b = buf; buf = []; setLines((l) => [...l, ...b].slice(-MAX)); };
    s.onopen = () => !dead && setConn("live");
    s.onmessage = (d) => {
      if (dead) return;
      for (const raw of String(typeof d === "string" ? d : new TextDecoder().decode(d)).split("\n")) {
        if (!raw) continue;
        const m = RE.exec(raw);
        buf.push({ n: ++seq.current, ts: m ? m[1] : fmtClock(new Date()), msg: m ? m[2] : raw });
      }
      if (!timer) timer = window.setTimeout(flush, 80);
    };
    s.onclose = () => !dead && setConn("closed");
    return () => { dead = true; window.clearTimeout(timer); s.close(); };
  }, [vm.id, running, nonce]);

  const shown = useMemo(() => {
    const base = frozen == null ? lines : lines.slice(0, frozen);
    const f = q.trim().toLowerCase();
    return f ? base.filter((l) => l.msg.toLowerCase().includes(f) || l.ts.includes(f)) : base;
  }, [lines, frozen, q]);

  useEffect(() => { if (frozen == null && el.current) el.current.scrollTop = el.current.scrollHeight; }, [shown, frozen]);

  if (!running) return <NeedsRunning vm={vm} what="Live logs" onResume={onResume} resuming={resuming} />;
  const following = frozen == null;
  return (
    <div className="vm-logs">
      <div className="vm-lgbar">
        <button className="btn sm" aria-pressed={following} onClick={() => setFrozen(following ? lines.length : null)}>{following ? "Pause" : "Follow"}</button>
        <input className="vm-search" type="search" placeholder="Search lines" aria-label="Search log lines" value={q} onChange={(e) => setQ(e.target.value)} />
        <span className="dim">{shown.length}{q ? ` of ${lines.length}` : ""} lines · {conn === "live" ? (following ? "following" : "paused") : conn === "connecting" ? "connecting…" : "stream closed"}</span>
        {conn === "closed" && <button className="btn sm" onClick={() => setNonce((n) => n + 1)}>Reconnect</button>}
      </div>
      <div className="vm-log" ref={el} role="log" aria-label="VM log lines">
        {shown.length ? shown.map((l) => <div key={l.n}><span className="ts">[{l.ts}]</span> <span>{l.msg}</span></div>) : <p className="vm-note">{lines.length ? "No lines match." : conn === "connecting" ? "Connecting…" : "No log lines yet."}</p>}
      </div>
    </div>
  );
}
