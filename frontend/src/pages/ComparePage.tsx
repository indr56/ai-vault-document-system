import { useCallback, useEffect, useState } from "react";
import { Empty, ErrorNote } from "../components/ui";
import { api, post, timeAgo } from "../lib/api";
import type { Comparison, DocumentItem, VaultEvent } from "../lib/types";

export default function ComparePage({ documents, lastEvent }: { documents: DocumentItem[]; lastEvent: VaultEvent | null }) {
  // Include older versions so "v1 vs v2" comparisons are possible.
  const [ready, setReady] = useState<DocumentItem[]>([]);
  useEffect(() => {
    api<{ items: DocumentItem[] }>("/documents?status=ready&include_old_versions=true&limit=200").then(
      (d) => setReady(d.items),
      setError,
    );
  }, [documents]);
  const [picked, setPicked] = useState<string[]>([]);
  const [focus, setFocus] = useState("");
  const [history, setHistory] = useState<Comparison[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    const items = await api<Comparison[]>("/comparisons");
    setHistory(items);
    return items;
  }, []);

  useEffect(() => {
    load().then((items) => items[0] && setActiveId((id) => id ?? items[0].id), setError);
  }, [load]);

  // Refresh when a comparison finishes (WebSocket) and poll as a fallback while one is running.
  useEffect(() => {
    if (lastEvent?.type.startsWith("comparison.")) void load();
  }, [lastEvent, load]);
  const running = history.some((c) => c.status === "pending" || c.status === "running");
  useEffect(() => {
    if (!running) return;
    const t = window.setInterval(load, 3000);
    return () => window.clearInterval(t);
  }, [running, load]);

  const toggle = (id: string) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id].slice(-5)));
  const start = async () => {
    setError(null);
    try {
      const c = await post<Comparison>("/comparisons", { document_ids: picked, focus });
      setActiveId(c.id);
      await load();
    } catch (e) {
      setError(e);
    }
  };

  const active = history.find((c) => c.id === activeId) ?? null;
  const titleOf = (id: string) => {
    const d = ready.find((x) => x.id === id);
    return d ? `${d.title}${d.version > 1 ? ` v${d.version}` : ""}` : id.slice(0, 8);
  };

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Compare</h1>
          <p>Compare 2–5 documents — e.g. two versions of a policy, or competing quotes. Runs in the background.</p>
        </div>
      </div>
      <div className="grid-2" style={{ gridTemplateColumns: "minmax(300px, 380px) 1fr", alignItems: "start" }}>
        <div className="stack">
          <div className="card card-pad stack">
            <h2>New comparison</h2>
            {ready.length < 2 ? (
              <Empty>Upload at least two documents first.</Empty>
            ) : (
              <div className="picker">
                {ready.map((d) => (
                  <label key={d.id}>
                    <input type="checkbox" checked={picked.includes(d.id)} onChange={() => toggle(d.id)} />
                    <span className="grow">
                      {d.title}
                      {d.version > 1 && <span className="muted small"> v{d.version}</span>}
                    </span>
                  </label>
                ))}
              </div>
            )}
            <input
              className="input"
              placeholder="Optional focus, e.g. “what changed at renewal”"
              value={focus}
              onChange={(e) => setFocus(e.target.value)}
            />
            <div className="row">
              <button className="btn primary" disabled={picked.length < 2} onClick={start}>
                Compare {picked.length >= 2 ? picked.length : ""} documents
              </button>
            </div>
            <ErrorNote error={error} />
          </div>
          <div className="card session-list">
            {history.map((c) => (
              <button key={c.id} className={`session-item ${c.id === activeId ? "active" : ""}`} onClick={() => setActiveId(c.id)}>
                <span style={{ fontWeight: 600 }}>{c.document_ids.map(titleOf).join(" vs ")}</span>
                <span className="muted small">
                  {c.status} · {c.focus || "general"} · {timeAgo(c.created_at)}
                </span>
              </button>
            ))}
          </div>
        </div>
        <div>{active ? <ComparisonView c={active} /> : <div className="card"><Empty>Pick documents to compare.</Empty></div>}</div>
      </div>
    </div>
  );
}

function ComparisonView({ c }: { c: Comparison }) {
  if (c.status === "pending" || c.status === "running")
    return (
      <div className="card card-pad row">
        <span className="spinner" /> Comparing documents… this usually takes 5–15 seconds.
      </div>
    );
  if (c.status === "failed" || !c.result) return <div className="alert">{c.error_message ?? "The comparison failed."}</div>;
  const r = c.result;
  const labels = Object.keys(r.labels);
  return (
    <div className="stack">
      <div className="card card-pad stack">
        <div className="row">
          {labels.map((l) => (
            <span key={l} className="badge">
              {l}: {r.labels[l].title}
            </span>
          ))}
        </div>
        <p className="prose" style={{ margin: 0 }}>
          {r.overview}
        </p>
      </div>
      {r.differences.length > 0 && (
        <div className="card">
          <table className="table">
            <thead>
              <tr>
                <th>Difference</th>
                {labels.map((l) => (
                  <th key={l}>{l}</th>
                ))}
                <th>Impact</th>
              </tr>
            </thead>
            <tbody>
              {r.differences.map((d, i) => (
                <tr key={i}>
                  <td>
                    <strong>{d.aspect}</strong>
                    <div className="muted small">{d.explanation}</div>
                  </td>
                  {labels.map((l) => {
                    const v = d.values.find((x) => x.document_label === l);
                    return (
                      <td key={l}>
                        {v?.value ?? "—"}
                        {v?.page ? <div className="muted small">p. {v.page}</div> : null}
                      </td>
                    );
                  })}
                  <td className={`sig-${d.significance}`} style={{ fontWeight: 650 }}>
                    {d.significance}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="grid-2">
        <ListCard title="Similarities" items={r.similarities} />
        <ListCard title="Gaps & risks" items={r.gaps_or_risks} />
      </div>
    </div>
  );
}

function ListCard({ title, items }: { title: string; items: string[] }) {
  return (
    <div className="card card-pad stack">
      <h3>{title}</h3>
      {items.length ? (
        <ul style={{ margin: 0, paddingLeft: 18 }}>
          {items.map((x, i) => (
            <li key={i}>{x}</li>
          ))}
        </ul>
      ) : (
        <span className="muted">None noted.</span>
      )}
    </div>
  );
}
