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

const DEMO_PROMPTS: { label: string; question: string }[] = [
  {
    label: "Classification",
    question:
      "How are the emails in this inbox classified? Give counts by document type (invoice, statement, payment reminder, remittance, escalation, final notice, other). For anything marked low confidence or error, say how many and why.",
  },
  {
    label: "Thomson invoice",
    question:
      "Show the key fields for Thomson Reuters invoice 853825322: vendor, invoice number, amount, currency, invoice date, due date, and confidence. Cite the message id. If a field is missing, say so.",
  },
  {
    label: "Punch invoices",
    question:
      "List Punch Distribution invoices you have extracted, with invoice number, amount, date, and confidence. Do not invent rows beyond what the tool returns, and say if the list is truncated.",
  },
  {
    label: "Punch chase",
    question:
      "For Punch Distribution, which chase emails do we have? For each, give the stage (reminder, escalation, or final notice), the invoice numbers they mention, whether that invoice is actually in the inbox, and your confidence. Cite message ids.",
  },
  {
    label: "Final notices",
    question:
      "List every final notice in the inbox. For each, give vendor, subject, confidence, and whether a chased invoice is present. If a “final notice” does not look like an unpaid AP invoice, say that.",
  },
  {
    label: "FPL chase",
    question:
      "Which FPL Communications chase emails refer to invoices that are not in the inbox? Give the stage and the referenced invoice numbers. If no invoice number was extracted, say that explicitly.",
  },
  {
    label: "Missing files",
    question:
      "Which attachments are missing from disk? Give fil_id, filename, and the error. Do not guess their contents.",
  },
  {
    label: "Unreadable PDFs",
    question:
      "Which invoice extractions failed because the PDF had no text? Quote the error detail and the confidence or status. Do not invent amounts for those files.",
  },
  {
    label: "Glass House PDF",
    question:
      "Find the Glass House Brands invoice PDF (filename contains Glass-House-Brands, fil_id fil_4xyrdndg). Check attachment status first. If it is ok, read the text and pull vendor, invoice number, amount, and dates. If you cannot read it, stop and say why.",
  },
  {
    label: "Low confidence",
    question:
      "Show messages classified as “other” with low confidence. How many are there, what confidence did we assign, and what is the rationale? Do not force them into invoice or reminder.",
  },
  {
    label: "Deliberate miss",
    question:
      "Is invoice ZZ-DOES-NOT-EXIST-999 in the inbox? Say whether you found it, and your confidence.",
  },
  {
    label: "Statements vs remittances",
    question:
      "Show a few emails classified as statements and a few classified as remittances. Include subject, confidence, and rationale. If a remittance label looks like a payment reminder, say so.",
  },
];

function gapClass(kind: string): string {
  const lower = kind.toLowerCase();
  if (
    lower.includes("missing") ||
    lower.includes("unreadable") ||
    lower.includes("error")
  ) {
    return `${styles.listItem} ${styles.listItemDanger}`;
  }
  if (
    lower.includes("low") ||
    lower.includes("unsure") ||
    lower.includes("no_match")
  ) {
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

  async function ask(nextQuestion?: string) {
    const trimmed = (nextQuestion ?? question).trim();
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
                <strong>Start with a demo question</strong>
                Each chip calls the read-only tool that holds the answer and
                lights a gap when something is missing, unreadable, low
                confidence, or not in the inbox.
                <div className={styles.chips}>
                  {DEMO_PROMPTS.map((prompt) => (
                    <button
                      key={prompt.label}
                      type="button"
                      className={styles.chip}
                      disabled={streaming}
                      onClick={() => void ask(prompt.question)}
                    >
                      {prompt.label}
                    </button>
                  ))}
                </div>
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

          {messages.length > 0 ? (
            <div className={styles.chips} aria-label="Demo prompts">
              {DEMO_PROMPTS.map((prompt) => (
                <button
                  key={prompt.label}
                  type="button"
                  className={styles.chip}
                  disabled={streaming}
                  onClick={() => void ask(prompt.question)}
                >
                  {prompt.label}
                </button>
              ))}
            </div>
          ) : null}

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
