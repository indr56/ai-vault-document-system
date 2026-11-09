import { useCallback, useEffect, useRef, useState } from "react";
import { Icon } from "./components/ui";
import { getApiKey, listAll, setApiKey } from "./lib/api";
import { useVaultEvents } from "./lib/events";
import type { DocumentItem, VaultEvent } from "./lib/types";
import ChatPage from "./pages/ChatPage";
import ComparePage from "./pages/ComparePage";
import DocumentsPage from "./pages/DocumentsPage";
import MetricsPage from "./pages/MetricsPage";

type Page = "documents" | "chat" | "compare" | "metrics";

const NAV: { id: Page; label: string; icon: "docs" | "chat" | "compare" | "chart" }[] = [
  { id: "documents", label: "Documents", icon: "docs" },
  { id: "chat", label: "Chat", icon: "chat" },
  { id: "compare", label: "Compare", icon: "compare" },
  { id: "metrics", label: "Monitoring", icon: "chart" },
];

export default function App() {
  // An unknown or empty #hash opens Documents (it used to render an empty page).
  const [page, setPage] = useState<Page>(() => {
    const fromHash = location.hash.slice(1) as Page;
    return NAV.some((n) => n.id === fromHash) ? fromHash : "documents";
  });
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [chatSeed, setChatSeed] = useState<string[] | null>(null);
  const [lastEvent, setLastEvent] = useState<VaultEvent | null>(null);

  useEffect(() => {
    const onHash = () => {
      const next = location.hash.slice(1) as Page;
      if (NAV.some((n) => n.id === next)) setPage(next);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const go = (next: Page) => {
    setPage(next);
    history.replaceState(null, "", `#${next}`);
  };

  // Every page of the list, not just the first 200 (an account can hold up to 10,000 documents).
  const refreshDocuments = useCallback(async () => {
    try {
      setDocuments(await listAll<DocumentItem>("/documents"));
      setLoadError(null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refreshDocuments();
  }, [refreshDocuments]);

  // Live updates: document events refresh the list; pages can react to the raw event too. Events come
  // in bursts (a 20-file batch sends ~100 stage events), so they are coalesced into one reload
  // 300 ms after the last one instead of one reload per event.
  const refreshTimer = useRef<number | undefined>(undefined);
  const connected = useVaultEvents((event) => {
    setLastEvent(event);
    if (!event.type.startsWith("document.")) return;
    window.clearTimeout(refreshTimer.current);
    refreshTimer.current = window.setTimeout(() => void refreshDocuments(), 300);
  });
  useEffect(() => () => window.clearTimeout(refreshTimer.current), []);

  // Fallback polling while something is in flight, in case the WebSocket is unavailable.
  const inFlight = documents.some((d) => d.status === "queued" || d.status === "processing" || d.insights_status === "pending");
  useEffect(() => {
    if (!inFlight || connected) return;
    const t = window.setInterval(refreshDocuments, 3000);
    return () => window.clearInterval(t);
  }, [inFlight, connected, refreshDocuments]);

  const startChat = (ids: string[]) => {
    setChatSeed(ids);
    go("chat");
  };

  const changeKey = () => {
    const key = prompt("API key (X-API-Key)", getApiKey());
    if (key) {
      setApiKey(key.trim());
      location.reload();
    }
  };

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark" /> Vault
        </div>
        {NAV.map((item) => (
          <button key={item.id} className={`nav-item ${page === item.id ? "active" : ""}`} onClick={() => go(item.id)}>
            <Icon name={item.icon} /> {item.label}
          </button>
        ))}
        <div className="sidebar-foot">
          <span className={`live ${connected ? "on" : ""}`}>{connected ? "Live updates on" : "Connecting…"}</span>
          <button className="btn sm" onClick={changeKey}>
            API key: {getApiKey().slice(0, 10)}…
          </button>
          <a href="/docs" target="_blank" rel="noreferrer">
            API docs ↗
          </a>
        </div>
      </aside>
      <main className="main">
        {loadError && (
          <div className="page">
            <div className="alert">Could not reach the API: {loadError}</div>
          </div>
        )}
        {page === "documents" && (
          <DocumentsPage documents={documents} refresh={refreshDocuments} onChat={startChat} lastEvent={lastEvent} />
        )}
        {page === "chat" && (
          <ChatPage documents={documents} seed={chatSeed} clearSeed={() => setChatSeed(null)} lastEvent={lastEvent} />
        )}
        {page === "compare" && <ComparePage documents={documents} lastEvent={lastEvent} connected={connected} />}
        {page === "metrics" && <MetricsPage lastEvent={lastEvent} />}
      </main>
    </div>
  );
}
