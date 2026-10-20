// Same-origin HTTP + WebSocket client. Works against the daemon (no auth), the control plane
// (Bearer token, `?token=` on WebSockets) or the in-browser mock backend (?mock / dev demo).
import { mockFetch, mockSocket } from "./mock";

export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) { super(detail || `HTTP ${status}`); this.status = status; this.detail = detail; }
}

const TOKEN_KEY = "flint.token";
export const getToken = () => localStorage.getItem(TOKEN_KEY) ?? "";
export const setToken = (t: string) => (t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY));

/** Mock mode: `?mock` (cluster-shaped demo) or `?mock=daemon`; remembered for the session. */
export function mockMode(): "" | "cluster" | "daemon" {
  const q = new URLSearchParams(location.search);
  if (q.has("mock")) sessionStorage.setItem("flint.mock", q.get("mock") === "daemon" ? "daemon" : "cluster");
  if (q.has("live")) sessionStorage.removeItem("flint.mock");
  return (sessionStorage.getItem("flint.mock") as "" | "cluster" | "daemon") ?? "";
}

type Query = Record<string, string | number | boolean | undefined | null>;
export interface ReqOpts { query?: Query; body?: unknown; raw?: BodyInit; headers?: Record<string, string>; as?: "json" | "blob" | "text"; idempotent?: boolean; signal?: AbortSignal }

function qs(q?: Query) {
  if (!q) return "";
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(q)) if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? "?" + s : "";
}

export async function request<T = unknown>(method: string, path: string, o: ReqOpts = {}): Promise<T> {
  const headers: Record<string, string> = { ...(o.headers ?? {}) };
  const tok = getToken();
  if (tok) headers.Authorization = `Bearer ${tok}`;
  let body: BodyInit | undefined = o.raw;
  if (o.body !== undefined) { headers["Content-Type"] = "application/json"; body = JSON.stringify(o.body); }
  if (o.idempotent) headers["Idempotency-Key"] = crypto.randomUUID();
  const url = path + qs(o.query);
  const res = mockMode() ? await mockFetch(method, url, headers, body) : await fetch(url, { method, headers, body, signal: o.signal });
  if (!res.ok) {
    let detail = res.statusText;
    try { const j = await res.json(); detail = j.detail ?? j.error ?? detail; } catch { /* not json */ }
    if (res.status === 401) window.dispatchEvent(new CustomEvent("flint:unauthorized"));
    throw new ApiError(res.status, String(detail));
  }
  if (o.as === "blob") return (await res.blob()) as T;
  if (o.as === "text") return (await res.text()) as T;
  const txt = await res.text();
  return (txt ? JSON.parse(txt) : {}) as T;
}

export const api = {
  get: <T>(p: string, o?: ReqOpts) => request<T>("GET", p, o),
  post: <T>(p: string, o?: ReqOpts) => request<T>("POST", p, o),
  put: <T>(p: string, o?: ReqOpts) => request<T>("PUT", p, o),
  patch: <T>(p: string, o?: ReqOpts) => request<T>("PATCH", p, o),
  del: <T>(p: string, o?: ReqOpts) => request<T>("DELETE", p, o),
};

/** Minimal socket surface shared by real and mock WebSockets. */
export interface Sock {
  onopen: (() => void) | null;
  onmessage: ((data: string | ArrayBuffer) => void) | null;
  onclose: ((code: number) => void) | null;
  send(data: string | ArrayBuffer | Uint8Array): void;
  close(): void;
}

export function openSocket(path: string, binary = false): Sock {
  if (mockMode()) return mockSocket(path);
  const tok = getToken();
  const sep = path.includes("?") ? "&" : "?";
  const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${path}${tok ? `${sep}token=${encodeURIComponent(tok)}` : ""}`;
  const ws = new WebSocket(url);
  if (binary) ws.binaryType = "arraybuffer";
  const s: Sock = {
    onopen: null, onmessage: null, onclose: null,
    send: (d) => { if (ws.readyState === WebSocket.OPEN) ws.send(d as string | ArrayBuffer); },
    close: () => ws.close(),
  };
  ws.onopen = () => s.onopen?.();
  ws.onmessage = (e) => s.onmessage?.(e.data);
  ws.onclose = (e) => s.onclose?.(e.code);
  return s;
}
