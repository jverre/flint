// Number, size, time and state formatting used across pages.

export const fmtMiB = (m: number | null | undefined): string =>
  m == null ? "—" : m >= 1024 ? (m / 1024).toFixed(m % 1024 ? 1 : 0) + " GiB" : Math.round(m) + " MiB";

export const fmtMinutes = (m: number | null | undefined): string => {
  if (!m || m < 0) return "—";
  if (m >= 1440) return `${Math.floor(m / 1440)}d ${Math.floor((m % 1440) / 60)}h`;
  if (m >= 60) return `${Math.floor(m / 60)}h ${String(Math.floor(m % 60)).padStart(2, "0")}m`;
  return `${Math.max(1, Math.floor(m))}m`;
};

/** Age since an epoch-seconds or ISO timestamp, compact ("3h 12m"). */
export const fmtSince = (t: number | string | null | undefined, now = Date.now()): string => {
  if (t == null) return "—";
  const ms = typeof t === "number" ? (t > 1e12 ? t : t * 1000) : Date.parse(t);
  if (!Number.isFinite(ms)) return "—";
  const s = Math.max(0, (now - ms) / 1000);
  return s < 60 ? `${Math.floor(s)}s` : fmtMinutes(s / 60);
};

export const fmtClock = (t: number | string | Date = new Date()): string => {
  const d = t instanceof Date ? t : new Date(typeof t === "number" ? (t > 1e12 ? t : t * 1000) : t);
  return d.toTimeString().slice(0, 8);
};

export const fmtMs = (ms: number | null | undefined): string => (ms == null ? "—" : ms >= 10000 ? (ms / 1000).toFixed(1) + " s" : Math.round(ms) + " ms");

export const pct = (a: number, b: number): string => (b ? Math.round((a / b) * 100) + " %" : "—");

export const STATE_LABEL: Record<string, string> = {
  running: "Running", paused: "Paused", starting: "Starting", error: "Failed", stopped: "Stopped", creating: "Starting", deleting: "Removing",
};
export const stateLabel = (s: string) => STATE_LABEL[s] ?? s;
