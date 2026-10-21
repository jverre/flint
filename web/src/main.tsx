import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import "@xterm/xterm/css/xterm.css";
import "./styles/theme.css";
import "./styles/kit.css";
import "./styles/app.css";
import App from "./App";
import { applyPrefs } from "./components/Shell";

applyPrefs();
const qc = new QueryClient({ defaultOptions: { queries: { staleTime: 2000, refetchOnWindowFocus: true } } });

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={qc}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
