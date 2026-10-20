import { useEffect, useState } from "react";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError, setToken } from "./api/client";
import { useEventStream, usePlatform } from "./api/hooks";
import Shell, { Cube } from "./components/Shell";
import { ToastProvider } from "./components/ui";
import VmsPage from "./pages/Vms";
import VmPage from "./pages/Vm";
import HostsPage from "./pages/Hosts";
import TenantsPage from "./pages/Tenants";
import TemplatesPage from "./pages/Templates";
import LoadTestPage from "./pages/LoadTest";

const router = createBrowserRouter(
  [
    {
      path: "/",
      element: <Shell />,
      children: [
        { index: true, element: <Navigate to="/vms" replace /> },
        { path: "vms", element: <VmsPage /> },
        { path: "vms/:id", element: <VmPage /> },
        { path: "vms/:id/:tab", element: <VmPage /> },
        { path: "tenants", element: <TenantsPage /> },
        { path: "tenants/:tenant", element: <TenantsPage /> },
        { path: "hosts", element: <HostsPage /> },
        { path: "templates", element: <TemplatesPage /> },
        { path: "loadtest", element: <LoadTestPage /> },
        { path: "*", element: <Navigate to="/vms" replace /> },
      ],
    },
  ],
  { basename: "/ui" },
);

export default function App() {
  const plat = usePlatform();
  const [unauth, setUnauth] = useState(false);
  useEffect(() => { const f = () => setUnauth(true); window.addEventListener("flint:unauthorized", f); return () => window.removeEventListener("flint:unauthorized", f); }, []);
  useEventStream(!!plat.data && !unauth);
  const err = plat.error;
  if (unauth || (err instanceof ApiError && err.status === 401)) return <Login />;
  if (plat.isLoading) return <Splash text="Connecting…" />;
  if (err) return <Splash text={`Can't reach Flint at ${location.host}: ${err.message}`} retry />;
  return <ToastProvider><RouterProvider router={router} /></ToastProvider>;
}

function Splash({ text, retry }: { text: string; retry?: boolean }) {
  return (
    <div className="splash">
      <Cube s={36} />
      <p>{text}</p>
      {retry && <button className="btn" onClick={() => location.reload()}>Try again</button>}
    </div>
  );
}

function Login() {
  const qc = useQueryClient();
  const [tok, setTok] = useState(""), [busy, setBusy] = useState(false), [err, setErr] = useState("");
  return (
    <div className="splash">
      <form className="login" onSubmit={async (e) => {
        e.preventDefault(); setBusy(true); setErr("");
        setToken(tok.trim());
        try { const r = await fetch("/vms", { headers: { Authorization: `Bearer ${tok.trim()}` } }); if (r.status === 401) throw new Error("That token was not accepted."); location.reload(); }
        catch (x) { setErr(String((x as Error).message)); setToken(""); setBusy(false); qc.clear(); }
      }}>
        <Cube s={32} />
        <h1>Sign in to Flint</h1>
        <p className="mut">This control plane needs an API token. Create one with <code>flint-cluster token create</code> or use the admin token it was started with.</p>
        <input type="password" autoFocus placeholder="flint_…" value={tok} onChange={(e) => setTok(e.target.value)} aria-label="API token" />
        {err && <p className="errc">{err}</p>}
        <button className="btn fill" disabled={!tok.trim() || busy}>Sign in</button>
        <p className="dim">The token is kept in this browser only.</p>
      </form>
    </div>
  );
}
