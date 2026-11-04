import type { ReactNode } from "react";
import type { DocumentItem } from "../lib/types";

export function StatusBadge({ doc }: { doc: Pick<DocumentItem, "status" | "stage" | "error"> }) {
  switch (doc.status) {
    case "ready":
      return <span className="badge ok">Ready</span>;
    case "failed":
      return (
        <span className="badge bad" title={doc.error?.message}>
          Failed
        </span>
      );
    case "processing":
      return (
        <span className="badge info">
          <span className="spinner" /> {doc.stage ? `${doc.stage[0].toUpperCase()}${doc.stage.slice(1)}…` : "Processing"}
        </span>
      );
    default:
      return (
        <span className="badge plain">
          <span className="spinner" /> Queued
        </span>
      );
  }
}

export function InsightsBadge({ status, stage }: { status: string; stage?: string | null }) {
  if (status === "ready") return <span className="badge ok">Insights</span>;
  if (status === "failed") return <span className="badge warn">No insights</span>;
  return (
    <span className="badge plain">
      {stage === "analyze" && <span className="spinner" />} Analyzing
    </span>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  return <div className="alert">{error instanceof Error ? error.message : String(error)}</div>;
}

export function Icon({ name }: { name: "docs" | "chat" | "compare" | "chart" }) {
  const paths: Record<string, ReactNode> = {
    docs: (
      <>
        <path d="M6 3h8l4 4v14H6z" />
        <path d="M14 3v4h4M9 12h6M9 16h6" />
      </>
    ),
    chat: <path d="M4 5h16v11H9l-5 4z" />,
    compare: (
      <>
        <path d="M4 4h7v16H4zM13 4h7v16h-7z" />
        <path d="M6.5 9h2M15.5 9h2M6.5 13h2M15.5 13h2" />
      </>
    ),
    chart: <path d="M4 20V10M10 20V4M16 20v-7M22 20H2" />,
  };
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      {paths[name]}
    </svg>
  );
}
