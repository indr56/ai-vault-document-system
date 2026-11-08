import { useEffect, useMemo, useRef, useState } from "react";

type Note = { id: number; kind: "info" | "error"; text: string };
let noteSeq = 0;
import { Empty, ErrorNote, InsightsBadge, StatusBadge } from "../components/ui";
import { api, fileTypesLabel, formatBytes, post, timeAgo, useUploadConfig } from "../lib/api";
import type { DocumentItem, Insights, ProcessingRun, SummaryResult, UploadResult, VaultEvent } from "../lib/types";

interface Props {
  documents: DocumentItem[];
  refresh: () => Promise<void>;
  onChat: (ids: string[]) => void;
  lastEvent: VaultEvent | null;
}

export default function DocumentsPage({ documents, refresh, onChat, lastEvent }: Props) {
  const uploadRules = useUploadConfig();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [notes, setNotes] = useState<Note[]>([]);
  const [extraDoc, setExtraDoc] = useState<DocumentItem | null>(null); // an older version opened from Versions
  const dismiss = (id: number) => setNotes((n) => n.filter((x) => x.id !== id));
  const pushNotes = (items: Omit<Note, "id">[]) => {
    const withIds = items.map((n) => ({ ...n, id: ++noteSeq }));
    setNotes((n) => [...n, ...withIds]);
    // Info messages stay 25 s, errors 30 s, long enough to read a batch result; ✕ closes one sooner.
    withIds.forEach((n) => window.setTimeout(() => dismiss(n.id), n.kind === "info" ? 25_000 : 30_000));
  };
  const [filter, setFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);

  const visible = useMemo(
    () =>
      documents.filter(
        (d) =>
          (!statusFilter || d.status === statusFilter) &&
          (!filter || `${d.title} ${d.original_filename}`.toLowerCase().includes(filter.toLowerCase())),
      ),
    [documents, filter, statusFilter],
  );
  const selected =
    documents.find((d) => d.id === selectedId) ?? (extraDoc && extraDoc.id === selectedId ? extraDoc : null);

  const upload = async (files: File[], replaces?: string) => {
    if (!files.length) return;
    setUploading(true);
    const next: Omit<Note, "id">[] = [];
    try {
      if (files.length === 1 || replaces) {
        const form = new FormData();
        form.append("file", files[0]);
        if (replaces) form.append("replaces_document_id", replaces);
        const res = await post<UploadResult>("/documents", form);
        next.push({ kind: "info", text: `${files[0].name}: ${res.message}` });
        if (replaces) setSelectedId(res.document.id);
        else if (res.duplicate) {
          // Show the document the file matched; it may be an older version the list hides.
          if (!res.document.is_latest) setExtraDoc(res.document);
          setSelectedId(res.document.id);
        }
      } else {
        const form = new FormData();
        files.forEach((f) => form.append("files", f));
        const res = await post<{ results: UploadResult[]; errors: { filename: string; message: string }[] }>(
          "/documents/batch",
          form,
        );
        res.results.forEach((r) => next.push({ kind: "info", text: `${r.document.original_filename}: ${r.message}` }));
        res.errors.forEach((e) => next.push({ kind: "error", text: `${e.filename}: ${e.message}` }));
      }
    } catch (e) {
      // Name the file, as batch errors do; a whole-batch failure (e.g. too many files) has no single file.
      const label = files.length === 1 || replaces ? `${files[0].name}: ` : "";
      next.push({ kind: "error", text: label + (e instanceof Error ? e.message : String(e)) });
    } finally {
      setUploading(false);
      pushNotes(next);
      await refresh();
    }
  };

  // Files can be dropped anywhere on the page, not only on the drop box.
  const uploadRef = useRef(upload);
  uploadRef.current = upload;
  useEffect(() => {
    let depth = 0;
    const hasFiles = (e: DragEvent) => Array.from(e.dataTransfer?.types ?? []).includes("Files");
    const onEnter = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      depth += 1;
      setDragging(true);
    };
    const onLeave = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      depth = Math.max(0, depth - 1);
      if (depth === 0) setDragging(false);
    };
    const onDrop = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth = 0;
      setDragging(false);
      void uploadRef.current(Array.from(e.dataTransfer?.files ?? []));
    };
    window.addEventListener("dragenter", onEnter);
    window.addEventListener("dragleave", onLeave);
    window.addEventListener("drop", onDrop);
    return () => {
      window.removeEventListener("dragenter", onEnter);
      window.removeEventListener("dragleave", onLeave);
      window.removeEventListener("drop", onDrop);
    };
  }, []);

  return (
    <div className="page">
      {dragging && <div className="drop-overlay">Drop files to upload</div>}
      <div className="page-head">
        <div>
          <h1>Documents</h1>
          <p>
            Upload {uploadRules ? fileTypesLabel(uploadRules) : "documents"}. Processing runs in the background; status
            updates live.
          </p>
        </div>
        <div className="row">
          <input className="input" placeholder="Search title…" value={filter} onChange={(e) => setFilter(e.target.value)} />
          <select className="select" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
            <option value="">All statuses</option>
            <option value="ready">Ready</option>
            <option value="processing">Processing</option>
            <option value="queued">Queued</option>
            <option value="failed">Failed</option>
          </select>
        </div>
      </div>

      <DropZone onFiles={(f) => upload(f)} busy={uploading} />
      {notes.map((n) => (
        <div key={n.id} className={`alert note ${n.kind === "info" ? "info" : ""}`}>
          <span>{n.text}</span>
          <button className="note-close" onClick={() => dismiss(n.id)} aria-label="Dismiss">
            ✕
          </button>
        </div>
      ))}

      <div className={`doc-layout ${selected ? "with-detail" : ""}`}>
        <div className="card">
          {visible.length === 0 ? (
            <Empty>{documents.length ? "No documents match the filter." : "No documents yet — drop a file above."}</Empty>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>Document</th>
                  <th>Status</th>
                  {!selected && <th>Details</th>}
                  <th>Added</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((d) => (
                  <tr
                    key={d.id}
                    className={`clickable ${d.id === selectedId ? "selected" : ""}`}
                    onClick={() => setSelectedId(d.id === selectedId ? null : d.id)}
                  >
                    <td>
                      <div className="doc-title">{d.title}</div>
                      <div className="muted small">
                        {d.extension.slice(1).toUpperCase()} · {formatBytes(d.size_bytes)}
                        {d.version > 1 && ` · v${d.version}`}
                      </div>
                    </td>
                    <td>
                      <div className="row">
                        <StatusBadge doc={d} />
                        {d.status === "ready" && <InsightsBadge status={d.insights_status} stage={d.stage} />}
                      </div>
                    </td>
                    {!selected && (
                      <td className="muted small">
                        {d.status === "failed"
                          ? d.error?.message
                          : d.status === "ready"
                            ? `${d.page_count ? `${d.page_count} pages · ` : ""}${d.chunk_count} chunk${d.chunk_count === 1 ? "" : "s"} · ${((d.processing_ms ?? 0) / 1000).toFixed(1)}s${d.warnings?.length ? " · ⚠ some pages not searchable" : ""}`
                            : ""}
                      </td>
                    )}
                    <td className="muted small">{timeAgo(d.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        {selected && (
          <DocumentDetail
            key={selected.id}
            doc={selected}
            lastEvent={lastEvent}
            onClose={() => setSelectedId(null)}
            onChat={() => onChat([selected.id])}
            onNewVersion={(f) => upload([f], selected.id)}
            onOpenVersion={(v) => {
              setExtraDoc(v);
              setSelectedId(v.id);
            }}
            onDeleted={async () => {
              setSelectedId(null);
              await refresh();
            }}
            refresh={refresh}
          />
        )}
      </div>
    </div>
  );
}

function DropZone({ onFiles, busy }: { onFiles: (files: File[]) => void; busy: boolean }) {
  const uploadRules = useUploadConfig();
  const [over, setOver] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  return (
    <div
      className={`dropzone ${over ? "over" : ""}`}
      onClick={() => input.current?.click()}
      onDragOver={(e) => {
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={() => setOver(false)} // the page-level handler uploads the files
    >
      <input
        ref={input}
        type="file"
        multiple
        hidden
        accept={uploadRules?.allowed_extensions.join(",")}
        onChange={(e) => {
          onFiles(Array.from(e.target.files ?? []));
          e.target.value = "";
        }}
      />
      {busy ? (
        <span className="row" style={{ justifyContent: "center" }}>
          <span className="spinner" /> Uploading…
        </span>
      ) : (
        <span>
          <strong>Choose files</strong> or drag them here
          {uploadRules && ` · ${fileTypesLabel(uploadRules)} · up to ${uploadRules.max_upload_mb} MB each`}
        </span>
      )}
    </div>
  );
}

type Tab = "insights" | "summary" | "versions" | "processing";

function DocumentDetail({
  doc,
  lastEvent,
  onClose,
  onChat,
  onNewVersion,
  onOpenVersion,
  onDeleted,
  refresh,
}: {
  doc: DocumentItem;
  lastEvent: VaultEvent | null;
  onClose: () => void;
  onChat: () => void;
  onNewVersion: (file: File) => void;
  onOpenVersion: (doc: DocumentItem) => void;
  onDeleted: () => Promise<void>;
  refresh: () => Promise<void>;
}) {
  const [tab, setTab] = useState<Tab>("insights");
  const [error, setError] = useState<unknown>(null);
  const uploadRules = useUploadConfig();
  const versionInput = useRef<HTMLInputElement>(null);

  const action = async (fn: () => Promise<unknown>) => {
    setError(null);
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError(e);
    }
  };

  return (
    <div className="card detail">
      <div className="card-pad stack">
        <div className="spread">
          <div>
            <h2>{doc.title}</h2>
            <div className="muted small">
              {doc.original_filename} · v{doc.version} · {formatBytes(doc.size_bytes)}
            </div>
          </div>
          <button className="btn ghost sm" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        <div className="row">
          <StatusBadge doc={doc} />
          {doc.status === "ready" && <InsightsBadge status={doc.insights_status} stage={doc.stage} />}
        </div>
        {!doc.is_latest && (
          <div className="alert info">You are viewing an older version (v{doc.version}). Chat and Compare can still use it.</div>
        )}
        {doc.error && (
          <div className="alert">
            <strong>{doc.error.code}</strong> — {doc.error.message}
          </div>
        )}
        {doc.warnings?.map((w) => (
          <div key={w.code} className="alert warn">
            {w.message}
          </div>
        ))}
        <div className="row">
          <button className="btn primary sm" disabled={doc.status !== "ready"} onClick={onChat}>
            Chat about this
          </button>
          <button className="btn sm" onClick={() => versionInput.current?.click()}>
            Upload new version
          </button>
          <input
            ref={versionInput}
            type="file"
            hidden
            accept={uploadRules?.allowed_extensions.join(",")}
            onChange={(e) => e.target.files?.[0] && onNewVersion(e.target.files[0])}
          />
          {(doc.status === "failed" || doc.status === "ready") && (
            <button className="btn sm" onClick={() => action(() => post(`/documents/${doc.id}/reprocess`))}>
              Reprocess
            </button>
          )}
          {doc.status === "ready" && doc.insights_status === "failed" && (
            <button className="btn sm" onClick={() => action(() => post(`/documents/${doc.id}/insights/regenerate`))}>
              Retry insights
            </button>
          )}
          <button
            className="btn danger sm"
            disabled={doc.status === "processing"}
            title={doc.status === "processing" ? "Available once processing finishes" : undefined}
            onClick={() =>
              confirm(`Delete "${doc.title}" (v${doc.version})?`) &&
              action(async () => {
                await api(`/documents/${doc.id}`, { method: "DELETE" });
                await onDeleted();
              })
            }
          >
            Delete
          </button>
        </div>
        <ErrorNote error={error} />
      </div>
      <div className="tabs">
        {(["insights", "summary", "versions", "processing"] as Tab[]).map((t) => (
          <button key={t} className={`tab ${tab === t ? "active" : ""}`} onClick={() => setTab(t)}>
            {t[0].toUpperCase() + t.slice(1)}
          </button>
        ))}
      </div>
      <div className="card-pad">
        {tab === "insights" && <InsightsTab doc={doc} lastEvent={lastEvent} />}
        {tab === "summary" && <SummaryTab doc={doc} />}
        {tab === "versions" && <VersionsTab doc={doc} onOpen={onOpenVersion} />}
        {tab === "processing" && <ProcessingTab doc={doc} lastEvent={lastEvent} />}
      </div>
    </div>
  );
}

function InsightsTab({ doc, lastEvent }: { doc: DocumentItem; lastEvent: VaultEvent | null }) {
  const [data, setData] = useState<Insights | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    api<Insights>(`/documents/${doc.id}/insights`).then(setData, setError);
  }, [doc.id, doc.insights_status, lastEvent?.type === "document.insights_ready" && lastEvent.document_id === doc.id]);

  if (error) return <ErrorNote error={error} />;
  if (!data) return <span className="muted">Loading…</span>;
  if (data.insights_status !== "ready")
    return (
      <Empty>
        {doc.status === "failed"
          ? "No insights: processing failed."
          : data.insights_status === "failed"
            ? "Insights could not be generated. Chat still works; use “Retry insights”."
            : "Insights are being generated…"}
      </Empty>
    );
  const entities = Object.entries(data.entities).filter(([, v]) => v.length);
  return (
    <div className="stack">
      <div className="row">
        {data.category && <span className="badge">{data.category}</span>}
        {data.document_type && <span className="badge plain">{data.document_type}</span>}
        {data.sentiment?.label && (
          <span className="badge plain" title={data.sentiment.rationale}>
            Tone: {data.sentiment.label}
          </span>
        )}
      </div>
      <p className="prose" style={{ margin: 0 }}>
        {data.summary}
      </p>
      {data.key_points.length > 0 && (
        <>
          <h3>Key points</h3>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {data.key_points.map((p, i) => (
              <li key={i}>{p}</li>
            ))}
          </ul>
        </>
      )}
      {data.key_facts.length > 0 && (
        <>
          <h3>Key facts</h3>
          <dl className="facts">
            {data.key_facts.map((f, i) => (
              <FactRow key={i} label={f.label} value={f.value} page={f.page} />
            ))}
          </dl>
        </>
      )}
      {entities.length > 0 && (
        <>
          <h3>Entities</h3>
          {entities.map(([kind, values]) => (
            <div key={kind} className="small">
              <span className="muted">{kind.replace("_", " ")}: </span>
              {values.join(" · ")}
            </div>
          ))}
        </>
      )}
      <h3>Tags</h3>
      <div className="chips">
        {data.tags.map((t) => (
          <span key={t} className="badge plain">
            {t}
          </span>
        ))}
      </div>
      <div className="muted small">
        {data.model} · {data.prompt_version}
      </div>
    </div>
  );
}

function FactRow({ label, value, page }: { label: string; value: string; page: number | null }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{value}</dd>
      <dd className="muted small">{page ? `p. ${page}` : ""}</dd>
    </>
  );
}

function SummaryTab({ doc }: { doc: DocumentItem }) {
  const [length, setLength] = useState("medium");
  const [tone, setTone] = useState("neutral");
  const [focus, setFocus] = useState("");
  const [result, setResult] = useState<SummaryResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      setResult(await post<SummaryResult>(`/documents/${doc.id}/summaries`, { length, tone, focus }));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="stack">
      <div className="row">
        <select className="select" value={length} onChange={(e) => setLength(e.target.value)}>
          <option value="short">Short</option>
          <option value="medium">Medium</option>
          <option value="long">Long</option>
        </select>
        <select className="select" value={tone} onChange={(e) => setTone(e.target.value)}>
          <option value="neutral">Neutral</option>
          <option value="executive">Executive</option>
          <option value="formal">Formal</option>
          <option value="simple">Plain language</option>
        </select>
      </div>
      <input
        className="input"
        placeholder="Optional focus, e.g. “deductibles and exclusions”"
        value={focus}
        onChange={(e) => setFocus(e.target.value)}
      />
      <div>
        <button className="btn primary" disabled={busy || doc.status !== "ready"} onClick={run}>
          {busy ? <span className="spinner" /> : null} Generate summary
        </button>
      </div>
      <ErrorNote error={error} />
      {result && (
        <div className="card card-pad">
          <div className="prose">{result.summary}</div>
          <div className="muted small" style={{ marginTop: 8 }}>
            {result.cached ? "Served from storage" : "Generated"} · {result.model}
          </div>
        </div>
      )}
    </div>
  );
}

function VersionsTab({ doc, onOpen }: { doc: DocumentItem; onOpen: (v: DocumentItem) => void }) {
  const [versions, setVersions] = useState<DocumentItem[] | null>(null);
  useEffect(() => {
    api<DocumentItem[]>(`/documents/${doc.id}/versions`).then(setVersions, () => setVersions([]));
  }, [doc.id, doc.status]);
  if (!versions) return <span className="muted">Loading…</span>;
  return (
    <table className="table">
      <tbody>
        {versions.map((v) => (
          <tr
            key={v.id}
            className={`clickable ${v.id === doc.id ? "selected" : ""}`}
            onClick={() => v.id !== doc.id && onOpen(v)}
            title={v.id === doc.id ? "Currently open" : "Open this version"}
          >
            <td>
              <strong>v{v.version}</strong> {v.is_latest && <span className="badge ok">latest</span>}
            </td>
            <td>{v.original_filename}</td>
            <td>
              <StatusBadge doc={v} />
            </td>
            <td className="muted small">{timeAgo(v.created_at)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const STAGE_COLORS: Record<string, string> = {
  parse: "#7c93b5",
  chunk: "#b59f7c",
  embed: "#5f8f7a",
  analyze: "#1f3a5f",
};

function ProcessingTab({ doc, lastEvent }: { doc: DocumentItem; lastEvent: VaultEvent | null }) {
  const [runs, setRuns] = useState<ProcessingRun[] | null>(null);
  useEffect(() => {
    api<ProcessingRun[]>(`/documents/${doc.id}/processing-runs`).then(setRuns, () => setRuns([]));
  }, [doc.id, doc.status, doc.insights_status, lastEvent]);
  if (!runs) return <span className="muted">Loading…</span>;
  if (!runs.length) return <Empty>Waiting for a worker…</Empty>;
  return (
    <div className="timeline">
      {runs.map((run) => {
        const total = run.stages.reduce((s, x) => s + (x.duration_ms ?? 0), 0) || 1;
        return (
          <div key={run.id} className="card card-pad stack">
            <div className="spread">
              <strong>Attempt {run.attempt}</strong>
              <span className={`badge ${run.status === "succeeded" ? "ok" : run.status === "failed" ? "bad" : "info"}`}>
                {run.status}
              </span>
            </div>
            <div className="stage-bar">
              {run.stages.map((s) => (
                <span
                  key={s.stage}
                  title={`${s.stage}: ${s.duration_ms} ms`}
                  style={{ width: `${((s.duration_ms ?? 0) / total) * 100}%`, background: STAGE_COLORS[s.stage] }}
                />
              ))}
            </div>
            <div className="row small">
              {run.stages.map((s) => (
                <span key={s.stage} className="muted">
                  <span style={{ color: STAGE_COLORS[s.stage] }}>■</span> {s.stage} {s.duration_ms} ms
                  {s.status === "failed" ? " ✘" : ""}
                </span>
              ))}
            </div>
            <div className="muted small">
              {run.duration_ms != null && `Total ${(run.duration_ms / 1000).toFixed(1)} s · `}
              {run.queue_wait_ms != null && `queued ${(run.queue_wait_ms / 1000).toFixed(1)} s · `}
              {timeAgo(run.started_at)}
              {run.error_message && ` · ${run.error_message}`}
            </div>
          </div>
        );
      })}
    </div>
  );
}
