// Small typed client for the Vault API. All requests carry the X-API-Key header.
import { useEffect, useState } from "react";

const KEY_STORAGE = "vault.apiKey";
export const API = "/api/v1";

export function getApiKey(): string {
  try {
    return localStorage.getItem(KEY_STORAGE) || "vault-demo-key";
  } catch {
    return "vault-demo-key";
  }
}

export function setApiKey(key: string): void {
  try {
    localStorage.setItem(KEY_STORAGE, key);
  } catch {
    /* storage unavailable: the key lives for this page only */
  }
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

async function parse<T>(response: Response): Promise<T> {
  if (response.status === 204) return undefined as T;
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const err = body?.error ?? {};
    throw new ApiError(response.status, err.code ?? "error", err.message ?? response.statusText);
  }
  return body as T;
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("X-API-Key", getApiKey());
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  return parse<T>(await fetch(`${API}${path}`, { ...init, headers }));
}

export const post = <T,>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body instanceof FormData ? body : JSON.stringify(body ?? {}) });

/** POST that reads a Server-Sent Events stream, calling onEvent for each event. */
export async function postStream(
  path: string,
  body: unknown,
  onEvent: (event: string, data: any) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { "X-API-Key": getApiKey(), "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok || !response.body) return void (await parse(response));
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r?\n\r?\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      const data: string[] = [];
      for (const line of frame.split(/\r?\n/)) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
      }
      if (data.length) onEvent(event, JSON.parse(data.join("\n")));
    }
  }
}

/** WebSocket URL with a single-use ticket: the API key itself never goes into a URL (logs). */
export async function eventsUrl(): Promise<string> {
  const { ticket } = await post<{ ticket: string }>("/events/ticket");
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}${API}/events?ticket=${encodeURIComponent(ticket)}`;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function timeAgo(iso: string): string {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(iso).toLocaleDateString();
}

/** Upload rules come from the API (GET /config), so a new file type or size limit is a backend-only change. */
export interface UploadConfig {
  allowed_extensions: string[];
  max_upload_mb: number;
  max_batch_files: number;
}

let uploadConfig: Promise<UploadConfig> | null = null;

export function useUploadConfig(): UploadConfig | null {
  const [config, setConfig] = useState<UploadConfig | null>(null);
  useEffect(() => {
    uploadConfig ??= api<{ upload: UploadConfig }>("/config").then((c) => c.upload);
    uploadConfig.then(setConfig, () => {
      uploadConfig = null; // retry on the next mount
    });
  }, []);
  return config;
}

/** ".pdf,.docx" -> "PDF, DOCX" */
export const fileTypesLabel = (c: UploadConfig) => c.allowed_extensions.map((e) => e.slice(1).toUpperCase()).join(", ");
