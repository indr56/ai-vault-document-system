import { useCallback, useEffect, useState } from "react";
import { ErrorNote } from "../components/ui";
import { api } from "../lib/api";
import type { VaultEvent } from "../lib/types";

type Json = any;

const pct = (v: number | null | undefined) => (v == null ? "—" : `${(v * 100).toFixed(1)}%`);
const ms = (v: number | null | undefined) => (v == null ? "—" : v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`);
const usd = (v: number | null | undefined) => (v == null ? "—" : v < 0.01 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`);
const num = (v: number | null | undefined) => (v == null ? "—" : v.toLocaleString());

export default function MetricsPage({ lastEvent }: { lastEvent: VaultEvent | null }) {
  const [days, setDays] = useState(7);
  const [data, setData] = useState<{ docs: Json; ai: Json; api: Json; system: Json } | null>(null);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    try {
      const [docs, ai, apiPerf, system] = await Promise.all([
        api<Json>(`/metrics/documents?days=${days}`),
        api<Json>(`/metrics/ai-usage?days=${days}`),
        api<Json>(`/metrics/api?hours=${Math.min(days * 24, 168)}`),
        api<Json>("/metrics/system"),
      ]);
      setData({ docs, ai, api: apiPerf, system });
      setError(null);
    } catch (e) {
      setError(e);
    }
  }, [days]);

  // Refresh every 15 s while the tab is visible; a hidden tab doesn't poll, and catches up when shown.
  useEffect(() => {
    void load();
    const t = window.setInterval(() => document.visibilityState === "visible" && void load(), 15000);
    const onVisible = () => document.visibilityState === "visible" && void load();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(t);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [load]);
  useEffect(() => {
    if (lastEvent?.type === "document.ready" || lastEvent?.type === "document.failed") void load();
  }, [lastEvent, load]);

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Monitoring</h1>
          <p>Processing health, AI usage and cost, chat quality and API performance. Refreshes every 15 s.</p>
        </div>
        <select className="select" value={days} onChange={(e) => setDays(Number(e.target.value))}>
          <option value={1}>Last 24 hours</option>
          <option value={7}>Last 7 days</option>
          <option value={30}>Last 30 days</option>
        </select>
      </div>
      <ErrorNote error={error} />
      {data && <Dashboard {...data} />}
    </div>
  );
}

function Dashboard({ docs, ai, api: apiPerf, system }: { docs: Json; ai: Json; api: Json; system: Json }) {
  const p = docs.processing;
  const stages: [string, Json][] = ["parse", "chunk", "embed", "analyze"]
    .filter((s) => p.stages[s])
    .map((s) => [s, p.stages[s]]);
  const maxStage = Math.max(1, ...stages.map(([, s]) => s.avg_ms ?? 0));
  const maxCost = Math.max(1e-9, ...ai.by_operation.map((o: Json) => o.cost_usd ?? 0));

  return (
    <>
      <div className="card card-pad health">
        <Health label="Database" ok={system.database?.ok} />
        <Health label="Redis" ok={system.redis?.ok} />
        <Health
          label="Worker"
          ok={system.worker?.ok}
          detail={system.worker?.last_heartbeat_seconds_ago != null ? `heartbeat ${system.worker.last_heartbeat_seconds_ago}s ago` : "no heartbeat"}
        />
        {system.queues && (
          <span className="badge plain">
            Queue depth: documents {system.queues.documents} · analysis {system.queues.analysis}
          </span>
        )}
      </div>

      {ai.unpriced_models?.length > 0 && (
        <div className="alert warn">
          No price configured for {ai.unpriced_models.join(", ")}: their cost is counted as $0. Add them to MODEL_PRICES.
        </div>
      )}
      <div className="tiles">
        <Tile label="Documents" value={num(docs.documents.total)} sub={`${num(docs.documents.total_pages)} pages · ${num(docs.documents.total_chunks)} chunks`} />
        <Tile label="Processing success" value={pct(p.success_rate)} sub={`${p.succeeded} ok · ${p.failed} failed`} />
        <Tile label="Processing time (p50 / p95)" value={ms(p.duration_ms.p50)} sub={`p95 ${ms(p.duration_ms.p95)} · queue wait ${ms(p.avg_queue_wait_ms)}`} />
        <Tile
          label="AI cost"
          value={usd(ai.totals.cost_usd)}
          sub={`${num(ai.totals.total_tokens)} tokens · ${ai.totals.calls} calls · ${ai.totals.failed_calls} failed`}
        />
        <Tile
          label="Saved by caches"
          value={usd(ai.cache_savings?.cost_saved_usd)}
          sub={`${num(ai.cache_savings?.tokens_saved)} tokens · ${num(ai.cache_savings?.hits)} hits`}
        />
        <Tile label="Chat latency (avg / p95)" value={ms(ai.chat.avg_latency_ms)} sub={`p95 ${ms(ai.chat.p95_latency_ms)} · ${ai.chat.answers} answers`} />
        <Tile label="Grounded answers" value={pct(ai.chat.grounded_rate)} sub={`abstained ${pct(ai.chat.abstention_rate)} · cache hits ${pct(ai.chat.cache_hit_rate)}`} />
      </div>

      <div className="grid-2">
        <div className="card card-pad stack">
          <h2>Pipeline stages (average)</h2>
          <div className="bars">
            {stages.map(([name, s]) => (
              <div key={name} className="bar-row">
                <span>
                  {name} {s.failed > 0 && <span className="badge bad">{s.failed} failed</span>}
                </span>
                <div className="bar-track">
                  <div className="bar-fill" style={{ width: `${((s.avg_ms ?? 0) / maxStage) * 100}%` }} />
                </div>
                <span className="num">{ms(s.avg_ms)}</span>
              </div>
            ))}
            {!stages.length && <span className="muted">No processing runs in this window.</span>}
          </div>
          {Object.keys(p.errors).length > 0 && (
            <>
              <h3>Errors</h3>
              <div className="row">
                {Object.entries(p.errors).map(([code, n]) => (
                  <span key={code} className="badge bad">
                    {code}: {String(n)}
                  </span>
                ))}
              </div>
            </>
          )}
        </div>

        <div className="card card-pad stack">
          <h2>AI cost by operation</h2>
          <div className="bars">
            {ai.by_operation.map((o: Json) => (
              <div key={o.operation} className="bar-row" title={`${o.calls} calls · ${o.tokens} tokens · avg ${ms(o.avg_latency_ms)}`}>
                <span>{o.operation.replace(/_/g, " ")}</span>
                <div className="bar-track">
                  <div className="bar-fill" style={{ width: `${((o.cost_usd ?? 0) / maxCost) * 100}%` }} />
                </div>
                <span className="num">{usd(o.cost_usd)}</span>
              </div>
            ))}
            {!ai.by_operation.length && <span className="muted">No AI calls in this window.</span>}
          </div>
          <div className="muted small">
            Models: {ai.by_model.map((m: Json) => `${m.model} (${m.calls})`).join(" · ") || "—"}
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-pad spread">
          <h2>API performance</h2>
          <span className="muted small">
            {num(apiPerf.requests)} requests · server error rate {pct(apiPerf.server_error_rate)}
          </span>
        </div>
        <table className="table">
          <thead>
            <tr>
              <th>Endpoint</th>
              <th className="num">Requests</th>
              <th className="num">p50</th>
              <th className="num">p95</th>
              <th className="num">4xx</th>
              <th className="num">5xx</th>
            </tr>
          </thead>
          <tbody>
            {apiPerf.routes.slice(0, 15).map((r: Json) => (
              <tr key={`${r.method} ${r.route}`}>
                <td className="mono">
                  {r.method} {r.route}
                </td>
                <td className="num">{r.requests}</td>
                <td className="num">{ms(r.p50_ms)}</td>
                <td className="num">{ms(r.p95_ms)}</td>
                <td className="num">{r.client_errors}</td>
                <td className="num">{r.server_errors}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function Tile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="card tile">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  );
}

function Health({ label, ok, detail }: { label: string; ok: boolean; detail?: string }) {
  return (
    <span className={`badge ${ok ? "ok" : "bad"}`} title={detail}>
      {ok ? "●" : "✕"} {label}
      {detail ? ` · ${detail}` : ""}
    </span>
  );
}
