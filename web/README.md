# Flint web UI

The admin UI served by Flint at `/ui` (daemon and control plane). React + TypeScript + Vite,
TanStack Query, react-router, Radix, xterm.js. Docs: [`docs/web-ui.mdx`](../docs/web-ui.mdx).

```bash
npm ci
npm run dev                         # http://localhost:5173/ui/ — proxies the API to FLINT_API (default http://127.0.0.1:9100)
open "http://localhost:5173/ui/?mock"   # built-in demo data (cluster-shaped); ?mock=daemon for a single host
../scripts/build-ui.sh              # typecheck + build into the daemon and the control plane
```

- `src/api/` — client (same-origin, Bearer token / `?token=` for the cluster), wire types, normalisation
  (one state mapping for REST, events and the cluster), data hooks + the live `/events` stream, and `mock.ts`,
  an in-browser implementation of the same API used by `?mock`.
- `src/components/` — app shell (sidebar, search, key hints) and shared UI pieces.
- `src/kit/` — SVG builders (charts, isometric host floor), formatting, keyboard model.
- `src/styles/` — `theme.css` (colour tokens, dark + light) and `kit.css` (the design system's classes).
- `src/pages/` — one folder per screen.

Rules the UI follows: facts the API reports, nothing inferred; keyboard shortcuts navigate but never change a
VM; destructive actions always confirm; colours only via theme tokens.
