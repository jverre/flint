import { Terminal } from "@xterm/xterm";
import { useEffect, useRef, useState } from "react";
import { openSocket, type Sock } from "../../api/client";
import type { VM } from "../../api/types";
import { token } from "../../kit/svg";
import { NeedsRunning, type TabProps } from "./common";

// The daemon PTY is fixed at 120x40 and has no resize protocol: keep xterm at that size and scale the box.
const COLS = 120, ROWS = 40;

function ShellTerm({ vm, visible }: { vm: VM; visible: boolean }) {
  const host = useRef<HTMLDivElement>(null), box = useRef<HTMLDivElement>(null), wrap = useRef<HTMLDivElement>(null);
  const term = useRef<Terminal | null>(null), sock = useRef<Sock | null>(null);
  const [conn, setConn] = useState<"connecting" | "connected" | "disconnected">("connecting");
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    const t = new Terminal({
      cols: COLS, rows: ROWS, fontSize: 12.5, fontFamily: token("--mono") || "monospace", cursorBlink: true, scrollback: 2000, allowProposedApi: true,
      theme: { background: token("--term-bg"), foreground: token("--term-fg"), cursor: token("--accent"), cursorAccent: token("--term-bg"), selectionBackground: token("--bg-selected"), blue: token("--term-prompt"), red: token("--term-err"), yellow: token("--term-warn") },
    });
    term.current = t;
    t.open(host.current!);
    const enc = new TextEncoder();
    t.onData((d) => sock.current?.send(enc.encode(d)));
    t.onBinary((d) => sock.current?.send(Uint8Array.from(d, (c) => c.charCodeAt(0))));
    const fit = () => {
      const w = wrap.current, b = box.current, el = host.current?.firstElementChild as HTMLElement | null;
      if (!w || !b || !el || !w.clientWidth) return;
      const nw = el.offsetWidth, nh = el.offsetHeight;
      if (!nw || !nh) return;
      const s = Math.min(1, w.clientWidth / nw, w.clientHeight / nh);
      host.current!.style.transform = `scale(${s})`;
      b.style.width = `${nw * s}px`; b.style.height = `${nh * s}px`;
    };
    const ro = new ResizeObserver(fit);
    ro.observe(wrap.current!);
    requestAnimationFrame(fit);
    return () => { ro.disconnect(); t.dispose(); term.current = null; };
  }, []);

  useEffect(() => {
    const t = term.current;
    if (!t) return;
    let dead = false;
    t.reset();
    setConn("connecting");
    const s = openSocket(`/vms/${vm.id}/terminal`, true);
    sock.current = s;
    s.onopen = () => { if (!dead) { setConn("connected"); if (visible) t.focus(); } };
    s.onmessage = (d) => { if (!dead) t.write(typeof d === "string" ? d : new Uint8Array(d)); };
    s.onclose = () => { if (dead) return; setConn("disconnected"); t.write("\r\n\x1b[2m[disconnected]\x1b[0m\r\n"); };
    return () => { dead = true; s.close(); if (sock.current === s) sock.current = null; };
  }, [vm.id, nonce]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { if (visible) { requestAnimationFrame(() => window.dispatchEvent(new Event("resize"))); term.current?.focus(); } }, [visible]);

  return (
    <div className="vm-shell">
      <div className="vm-tline" role="status">
        <span className={`vm-conn ${conn}`}><span className="d" />{conn === "connected" ? "Connected" : conn === "connecting" ? "Connecting…" : "Disconnected"}</span>
        {conn === "disconnected" && <button className="btn sm" onClick={() => setNonce((n) => n + 1)}>Reconnect</button>}
        <span className="dim">Every connection starts a new shell; there is no scrollback or reattach. Terminal size is fixed at {COLS}×{ROWS}.</span>
      </div>
      <div className="vm-term" ref={wrap} onMouseDown={() => setTimeout(() => term.current?.focus(), 0)}>
        <div className="vm-termbox" ref={box}><div className="vm-termhost" ref={host} /></div>
      </div>
      <p className="vm-note dim">Credentials are added by the proxy outside the VM and are never visible inside it, so <code>env | grep TOKEN</code> is empty on purpose.</p>
    </div>
  );
}

export default function ShellTab({ vm, running, onResume, resuming, active }: TabProps & { active: boolean }) {
  if (!running) return <NeedsRunning vm={vm} what="A shell" onResume={onResume} resuming={resuming} />;
  return <ShellTerm vm={vm} visible={active} />;
}
