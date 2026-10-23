import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import "@xterm/xterm/css/xterm.css";
import "./styles/theme.css";
import "./styles/kit.css";
import "./styles/app.css";
import App from "./App";
import { setToken } from "./api/client";
import { applyPrefs } from "./components/Shell";

// `flint ui` opens /ui/#token=…: keep the token, then drop it from the address bar and history.
const fromLink = new URLSearchParams(location.hash.slice(1)).get("token");
if (fromLink) { setToken(fromLink); history.replaceState(null, "", location.pathname + location.search); }

applyPrefs();
const qc = new QueryClient({ defaultOptions: { queries: { staleTime: 2000, refetchOnWindowFocus: true } } });

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={qc}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
