// CSS 3D cube. "fly" = indeterminate assembly loop (a request is in flight), "lit" = ready,
// "queued" = waiting for a free slot, "err" = the request failed. No JS animation: reduced motion is handled in newvm.css.
const FACES = ["bottom", "back", "left", "right", "front", "top"] as const;
export type CubeState = "queued" | "fly" | "lit" | "err";

export function Cube({ state, size = 120, label }: { state: CubeState; size?: number; label?: string }) {
  return (
    <div className="nv-scene" style={{ width: size * 1.75, height: size * 1.75 }} role="img" aria-label={label ?? state}>
      <div className={`nv-cube ${state}`} style={{ ["--s" as string]: `${size}px` }}>
        {FACES.map((f) => <i key={f} className={`f ${f}`} />)}
      </div>
    </div>
  );
}
