import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { Empty, ErrorNote } from "../components/ui";
import { api, listAll, post, postStream, timeAgo } from "../lib/api";
import { answerPieces } from "../lib/format";
import type { ChatSession, Citation, DocumentItem, Message, VaultEvent } from "../lib/types";

interface Props {
  documents: DocumentItem[];
  seed: string[] | null;
  clearSeed: () => void;
  lastEvent: VaultEvent | null;
}

export default function ChatPage({ documents, seed, clearSeed, lastEvent }: Props) {
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const loadSessions = useCallback(async () => {
    const items = await listAll<ChatSession>("/chat/sessions", 100); // all of them, newest first
    setSessions(items);
    return items;
  }, []);

  const createSession = useCallback(
    async (ids: string[]) => {
      setError(null);
      try {
        const session = await post<ChatSession>("/chat/sessions", { document_ids: ids });
        await loadSessions();
        setActiveId(session.id);
        setCreating(false);
      } catch (e) {
        setError(e);
      }
    },
    [loadSessions],
  );

  // After a chat is deleted (here or in another tab), open the newest remaining one, or the picker.
  const showAfterDelete = useCallback((items: ChatSession[], deletedId: string) => {
    setActiveId((current) => (current === deletedId || current === null ? (items[0]?.id ?? null) : current));
    if (!items.length) setCreating(true);
  }, []);

  const deleteSession = async (id: string) => {
    setError(null);
    try {
      await api(`/chat/sessions/${id}`, { method: "DELETE" });
      showAfterDelete(await loadSessions(), id);
    } catch (e) {
      setError(e);
    }
  };

  // Live: chats created, deleted or answered in another tab show up here without a reload.
  useEffect(() => {
    if (!lastEvent?.type.startsWith("chat.")) return;
    loadSessions().then((items) => {
      if (lastEvent.type === "chat.session_deleted" && lastEvent.session_id) showAfterDelete(items, lastEvent.session_id);
    }, setError);
  }, [lastEvent, loadSessions, showAfterDelete]);

  useEffect(() => {
    loadSessions().then((items) => {
      if (seed) {
        void createSession(seed);
        clearSeed();
      } else if (items.length && !activeId) setActiveId(items[0].id);
      else if (!items.length) setCreating(true);
    }, setError);
  }, []);

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Chat</h1>
          <p>Ask questions across one or more documents. Every answer cites the document and page it came from.</p>
        </div>
        <button className="btn primary" onClick={() => setCreating(true)}>
          New chat
        </button>
      </div>
      <ErrorNote error={error} />
      <div className="chat-layout">
        <div className="card session-list">
          {sessions.length === 0 && <div className="muted small" style={{ padding: 10 }}>No conversations yet.</div>}
          {sessions.map((s) => (
            <button
              key={s.id}
              className={`session-item ${s.id === activeId && !creating ? "active" : ""}`}
              onClick={() => {
                setActiveId(s.id);
                setCreating(false);
              }}
            >
              <span style={{ fontWeight: 600 }}>{s.title}</span>
              <span className="muted small">
                {s.documents.length} doc{s.documents.length > 1 ? "s" : ""} · {s.message_count} msgs ·{" "}
                {timeAgo(s.updated_at)}
              </span>
            </button>
          ))}
        </div>
        {creating || !activeId ? (
          <NewChat documents={documents} onCreate={createSession} />
        ) : (
          <Conversation
            key={activeId}
            sessionId={activeId}
            onChanged={loadSessions}
            onDelete={() => deleteSession(activeId)}
            lastEvent={lastEvent}
          />
        )}
      </div>
    </div>
  );
}

function NewChat({ documents, onCreate }: { documents: DocumentItem[]; onCreate: (ids: string[]) => void }) {
  const ready = documents.filter((d) => d.status === "ready");
  const [picked, setPicked] = useState<string[]>([]);
  const toggle = (id: string) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));
  return (
    <div className="card card-pad stack" style={{ alignContent: "start" }}>
      <h2>Choose documents to chat with</h2>
      {ready.length === 0 ? (
        <Empty>No ready documents yet. Upload one on the Documents page.</Empty>
      ) : (
        <div className="picker">
          {ready.map((d) => (
            <label key={d.id}>
              <input type="checkbox" checked={picked.includes(d.id)} onChange={() => toggle(d.id)} />
              <span className="grow">
                <strong>{d.title}</strong>
                <span className="muted small"> · {d.extension.slice(1).toUpperCase()}{d.version > 1 && ` · v${d.version}`}</span>
              </span>
            </label>
          ))}
        </div>
      )}
      <div>
        <button className="btn primary" disabled={!picked.length} onClick={() => onCreate(picked)}>
          Start chat with {picked.length || ""} document{picked.length === 1 ? "" : "s"}
        </button>
      </div>
    </div>
  );
}

interface Pending {
  question: string;
  answer: string;
  stage: string;
}

interface SkippedDocument {
  document_id: string;
  title: string;
  status: string;
}

function Conversation({
  sessionId,
  onChanged,
  onDelete,
  lastEvent,
}: {
  sessionId: string;
  onChanged: () => void;
  onDelete: () => void;
  lastEvent: VaultEvent | null;
}) {
  const [session, setSession] = useState<ChatSession | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [pending, setPending] = useState<Pending | null>(null);
  const [skipped, setSkipped] = useState<SkippedDocument[]>([]);
  const [error, setError] = useState<unknown>(null);
  const scroller = useRef<HTMLDivElement>(null);

  const loadHistory = useCallback(
    () =>
      api<{ session: ChatSession; messages: Message[] }>(`/chat/sessions/${sessionId}`).then((d) => {
        setSession(d.session);
        setMessages(d.messages);
        return d.messages;
      }, setError),
    [sessionId],
  );

  useEffect(() => {
    void loadHistory();
  }, [loadHistory]);

  // An answer saved in another tab for this chat: show it (our own answers arrive via the stream).
  useEffect(() => {
    if (lastEvent?.type === "chat.message_created" && lastEvent.session_id === sessionId && !pending) void loadHistory();
  }, [lastEvent, sessionId, pending, loadHistory]);

  // One of this chat's documents was deleted: refresh its document list (and the empty-chat note).
  useEffect(() => {
    if (lastEvent?.type === "document.deleted" && session?.documents.some((d) => d.id === lastEvent.document_id))
      void loadHistory();
  }, [lastEvent, session, loadHistory]);
  const orphaned = session !== null && session.documents.length === 0;

  useEffect(() => {
    if (scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight;
  }, [messages, pending?.answer]);

  const ask = async (question: string) => {
    question = question.trim();
    if (!question || pending) return;
    setInput("");
    setError(null);
    setPending({ question, answer: "", stage: "Searching documents…" });
    let ended = false; // a "done" or "error" event arrived
    try {
      await postStream(`/chat/sessions/${sessionId}/messages/stream`, { question }, (event, data) => {
        if (event === "status") {
          setPending((p) => p && { ...p, stage: data.stage === "retrieving" ? "Searching documents…" : "Writing answer…" });
          if (data.skipped_documents) setSkipped(data.skipped_documents);
        }
        else if (event === "token") setPending((p) => p && { ...p, answer: p.answer + data.text });
        else if (event === "done") {
          ended = true;
          setMessages((m) => [...m, data.question, data.answer]);
          setPending(null);
        } else if (event === "error") {
          ended = true;
          setError(new Error(data.message));
          setPending(null);
          void loadHistory(); // the question and any partial answer were saved
        }
      });
    } catch (e) {
      setError(e);
    } finally {
      setPending(null);
      onChanged();
      // The stream stopped without "done" or "error" (connection dropped, proxy timeout). Once the
      // answer has started, the server saves the question and the partial answer: show them. If it
      // dropped before that, nothing was saved: put the question back so it isn't lost.
      if (!ended)
        void loadHistory().then((saved) => {
          const last = saved?.filter((m) => m.role === "user").at(-1);
          if (last?.content === question) return;
          setInput(question);
          setError(new Error("The connection dropped before the answer started. Your question is back in the box."));
        });
    }
  };

  const lastAssistant = [...messages].reverse().find((m) => m.role === "assistant");

  return (
    <div className="card chat-panel">
      <div className="chat-head">
        <div className="spread">
          <h2>{session?.title ?? "…"}</h2>
          <button
            className="btn danger sm"
            disabled={!!pending}
            title={pending ? "Available once the answer finishes" : undefined}
            onClick={() => confirm(`Delete the chat "${session?.title ?? ""}" and its messages?`) && onDelete()}
          >
            Delete chat
          </button>
        </div>
        <div className="row small muted" style={{ marginTop: 4 }}>
          {session?.documents.map((d) => (
            <span key={d.id} className="badge plain">
              {d.title}
              {d.version > 1 && ` v${d.version}`}
            </span>
          ))}
        </div>
      </div>
      <div className="messages" ref={scroller}>
        {messages.length === 0 && !pending && (
          <Empty>Ask anything about these documents — e.g. “What are the deductibles?”</Empty>
        )}
        {messages.map((m) => (m.role === "user" ? <UserMsg key={m.id} text={m.content} /> : <AssistantMsg key={m.id} m={m} />))}
        {pending && (
          <>
            <UserMsg text={pending.question} />
            <div className="msg assistant">
              <div className="bubble">
                {pending.answer ? (
                  <span className="prose cursor">
                    {answerPieces(pending.answer).map((piece, i) => {
                      const text = piece.kind === "cite" ? piece.mark : piece.text;
                      return piece.bold ? <strong key={i}>{text}</strong> : <Fragment key={i}>{text}</Fragment>;
                    })}
                  </span>
                ) : (
                  <span className="row muted">
                    <span className="spinner" /> {pending.stage}
                  </span>
                )}
              </div>
            </div>
          </>
        )}
        {!pending && !orphaned && lastAssistant && lastAssistant.follow_up_questions.length > 0 && (
          <div className="chips">
            {lastAssistant.follow_up_questions.map((q) => (
              <button key={q} className="chip" onClick={() => ask(q)}>
                {q}
              </button>
            ))}
          </div>
        )}
        {orphaned && (
          <div className="alert info">
            The documents in this chat were deleted, so it can't answer new questions. Start a new chat to ask about
            other documents.
          </div>
        )}
        {skipped.length > 0 && (
          <div className="alert info">
            Not used for this answer (not ready yet): {skipped.map((d) => `${d.title} (${d.status})`).join(", ")}
          </div>
        )}
        <ErrorNote error={error} />
      </div>
      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          void ask(input);
        }}
      >
        <textarea
          className="textarea"
          rows={1}
          placeholder={orphaned ? "This chat's documents were deleted" : "Ask a question…  (Enter to send, Shift+Enter for a new line)"}
          disabled={orphaned}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void ask(input);
            }
          }}
        />
        <button className="btn primary" type="submit" disabled={!input.trim() || !!pending || orphaned}>
          Send
        </button>
      </form>
    </div>
  );
}

function UserMsg({ text }: { text: string }) {
  return <div className="msg user prose">{text}</div>;
}

function AssistantMsg({ m }: { m: Message }) {
  const [active, setActive] = useState<number | null>(null);
  const byNumber = new Map(m.citations.map((c) => [c.number, c]));
  return (
    <div className="msg assistant">
      <div className={`bubble ${m.abstained ? "abstained" : ""}`}>
        <div className="prose">
          {answerPieces(m.content).map((piece, i) => {
            const citation = piece.kind === "cite" ? byNumber.get(piece.number) : undefined;
            if (!citation) {
              const text = piece.kind === "cite" ? piece.mark : piece.text;
              return piece.bold ? <strong key={i}>{text}</strong> : <Fragment key={i}>{text}</Fragment>;
            }
            return (
              <button
                key={i}
                className={`cite ${active === citation.number ? "active" : ""}`}
                title={`${citation.document_title}${citation.page_number ? `, p. ${citation.page_number}` : ""}`}
                onClick={() => setActive(active === citation.number ? null : citation.number)}
              >
                {citation.number}
              </button>
            );
          })}
        </div>
        {m.citations.length > 0 && (
          <div className="sources">
            {m.citations.map((c) => (
              <SourceCard key={c.number} c={c} active={active === c.number} onClick={() => setActive(c.number)} />
            ))}
          </div>
        )}
      </div>
      <div className="row small muted" style={{ marginTop: 4 }}>
        {m.abstained ? <span className="badge warn">Not found in documents</span> : m.grounded ? <span className="badge ok">Grounded</span> : null}
        {m.from_cache && (
          <span
            className="badge plain"
            title={m.cache_match ? `Reused the answer to: “${m.cache_match.question}”` : "Reused an earlier answer"}
          >
            {m.cache_match?.type === "similar"
              ? `Cached · similar question (${Math.round(m.cache_match.similarity * 100)}% match)`
              : "Cached"}
          </span>
        )}
        {m.latency_ms != null && <span>{(m.latency_ms / 1000).toFixed(1)} s</span>}
        {m.standalone_question && m.standalone_question !== "" && (
          <span title="How the follow-up was interpreted for search">↳ searched: “{m.standalone_question}”</span>
        )}
      </div>
    </div>
  );
}

function SourceCard({ c, active, onClick }: { c: Citation; active: boolean; onClick: () => void }) {
  return (
    <div className={`source ${active ? "active" : ""}`} onClick={onClick}>
      <div className="spread">
        <strong>
          [{c.number}] {c.document_title}
        </strong>
        <span className="muted small">{c.page_number ? `page ${c.page_number}` : "no pages in this file type"}</span>
      </div>
      {active && <div className="muted" style={{ marginTop: 4 }}>{c.snippet}</div>}
    </div>
  );
}
