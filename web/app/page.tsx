"use client";

import { FormEvent, useRef, useState } from "react";
import {
  CitationEvent,
  GapEvent,
  streamChat,
} from "@/lib/sse";
import styles from "./page.module.css";

type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
};

function formatConfidence(value: number | null): string {
  if (value === null) return "—";
  return `${Math.round(value * 100)}%`;
}

function gapClass(kind: string): string {
  const lower = kind.toLowerCase();
  if (
    lower.includes("missing") ||
    lower.includes("unreadable") ||
    lower.includes("error")
  ) {
    return `${styles.listItem} ${styles.listItemDanger}`;
  }
  if (lower.includes("low") || lower.includes("unsure")) {
    return `${styles.listItem} ${styles.listItemWarn}`;
  }
  return styles.listItem;
}

export default function HomePage() {
  const [question, setQuestion] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [confidence, setConfidence] = useState<number | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [gaps, setGaps] = useState<GapEvent[]>([]);
  const [citations, setCitations] = useState<CitationEvent[]>([]);
  const [error, setError] = useState<string | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const assistantIdRef = useRef<string | null>(null);

  function appendAssistantToken(text: string) {
    const id = assistantIdRef.current;
    if (!id) return;
    setMessages((prev) =>
      prev.map((m) =>
        m.id === id ? { ...m, content: m.content + text } : m
      )
    );
  }

  async function ask() {
    const trimmed = question.trim();
    if (!trimmed || streaming) return;

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    const userId = `u-${Date.now()}`;
    const assistantId = `a-${Date.now()}`;
    assistantIdRef.current = assistantId;

    setMessages((prev) => [
      ...prev,
      { id: userId, role: "user", content: trimmed },
      { id: assistantId, role: "assistant", content: "" },
    ]);
    setQuestion("");
    setStreaming(true);
    setStatus("Connecting…");
    setConfidence(null);
    setRunId(null);
    setGaps([]);
    setCitations([]);
    setError(null);

    try {
      await streamChat({
        question: trimmed,
        signal: controller.signal,
        handlers: {
          onStatus: (data) => {
            setStatus(
              data.detail ? `${data.stage}: ${data.detail}` : data.stage
            );
          },
          onToken: (data) => {
            setStatus("Streaming answer…");
            appendAssistantToken(data.text);
          },
          onCitation: (data) => {
            setCitations((prev) => [...prev, data]);
          },
          onGap: (data) => {
            setGaps((prev) => [...prev, data]);
          },
          onDone: (data) => {
            setConfidence(data.answer_confidence);
            setRunId(data.run_id);
            setStatus(null);
          },
          onError: (data) => {
            setError(
              data.recoverable
                ? `${data.message} (recoverable)`
                : data.message
            );
            setStatus(null);
          },
        },
      });
    } catch (err) {
      if ((err as Error).name === "AbortError") {
        setStatus(null);
        setError("Request cancelled");
      } else {
        setError(
          err instanceof Error ? err.message : "Failed to reach chat stream"
        );
        setStatus(null);
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    void ask();
  }

  function handleStop() {
    abortRef.current?.abort();
  }

  return (
    <div className={styles.shell}>
      <header className={styles.header}>
        <h1 className={styles.brand}>AP Inbox Agent</h1>
        <p className={styles.tagline}>
          Ask about invoices, vendor chase threads, and attachment gaps — answers
          stream live with confidence and missing-data signals.
        </p>
      </header>

      <div className={styles.main}>
        <section className={styles.chatPanel} aria-label="Chat">
          <div className={styles.statusRow} aria-live="polite">
            {streaming && status ? (
              <>
                <span className={styles.statusDot} aria-hidden />
                <span>{status}</span>
              </>
            ) : null}
          </div>

          <div className={styles.messages}>
            {messages.length === 0 ? (
              <div className={styles.empty}>
                <strong>Start with a question</strong>
                Try “Which vendors sent final notices last week?” or “Is invoice
                X-863935 in the inbox?”
              </div>
            ) : (
              messages.map((m) => (
                <div
                  key={m.id}
                  className={`${styles.bubble} ${
                    m.role === "user"
                      ? styles.bubbleUser
                      : styles.bubbleAssistant
                  }`}
                >
                  {m.content ||
                    (streaming && m.id === assistantIdRef.current
                      ? ""
                      : "…")}
                  {streaming &&
                    m.role === "assistant" &&
                    m.id === assistantIdRef.current && (
                      <span className={styles.caret} aria-hidden />
                    )}
                </div>
              ))
            )}
          </div>

          <form className={styles.composer} onSubmit={handleSubmit}>
            <textarea
              className={styles.input}
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder="Ask the AP inbox…"
              rows={2}
              disabled={streaming}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void ask();
                }
              }}
              aria-label="Question"
            />
            {streaming ? (
              <button
                type="button"
                className={styles.stop}
                onClick={handleStop}
              >
                Stop
              </button>
            ) : (
              <button
                type="submit"
                className={styles.send}
                disabled={!question.trim()}
              >
                Ask
              </button>
            )}
          </form>
        </section>

        <aside className={styles.sidePanel} aria-label="Stream metadata">
          <div className={styles.sideBlock}>
            <h2>Confidence</h2>
            <div className={styles.confidence}>
              <span className={styles.confidenceValue}>
                {formatConfidence(confidence)}
              </span>
              <span className={styles.confidenceHint}>answer</span>
            </div>
            {runId ? (
              <p className={styles.meta}>run {runId}</p>
            ) : (
              <p className={styles.meta}>No completed run yet</p>
            )}
          </div>

          {error ? (
            <div className={styles.errorBanner} role="alert">
              {error}
            </div>
          ) : null}

          <div className={styles.sideBlock}>
            <h2>Gaps ({gaps.length})</h2>
            {gaps.length === 0 ? (
              <p className={styles.meta}>None reported for this answer</p>
            ) : (
              <ul className={styles.list}>
                {gaps.map((g, i) => (
                  <li key={`${g.kind}-${i}`} className={gapClass(g.kind)}>
                    <div className={styles.itemKind}>{g.kind}</div>
                    {g.detail ? (
                      <div className={styles.itemDetail}>{g.detail}</div>
                    ) : null}
                    {(g.fil_id || g.message_id) && (
                      <div className={styles.itemIds}>
                        {[g.message_id && `msg ${g.message_id}`, g.fil_id]
                          .filter(Boolean)
                          .join(" · ")}
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className={styles.sideBlock}>
            <h2>Citations ({citations.length})</h2>
            {citations.length === 0 ? (
              <p className={styles.meta}>None yet</p>
            ) : (
              <ul className={styles.list}>
                {citations.map((c, i) => (
                  <li
                    key={`${c.message_id}-${i}`}
                    className={styles.listItem}
                  >
                    <div className={styles.itemKind}>
                      msg {c.message_id} ·{" "}
                      {formatConfidence(c.confidence)}
                    </div>
                    {c.note ? (
                      <div className={styles.itemDetail}>{c.note}</div>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </aside>
      </div>
    </div>
  );
}
