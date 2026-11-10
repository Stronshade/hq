/** SSE event payloads from POST /chat/stream */

export type StatusEvent = {
  stage: string;
  detail: string;
};

export type TokenEvent = {
  text: string;
};

export type CitationEvent = {
  message_id: string;
  confidence: number;
  note: string;
};

export type GapEvent = {
  kind: string;
  fil_id?: string;
  message_id?: string;
  detail?: string;
};

export type DoneEvent = {
  answer_confidence: number;
  run_id: string;
};

export type ErrorEvent = {
  message: string;
  recoverable: boolean;
};

export type ChatStreamHandlers = {
  onStatus?: (data: StatusEvent) => void;
  onToken?: (data: TokenEvent) => void;
  onCitation?: (data: CitationEvent) => void;
  onGap?: (data: GapEvent) => void;
  onDone?: (data: DoneEvent) => void;
  onError?: (data: ErrorEvent) => void;
};

export type StreamChatOptions = {
  apiUrl?: string;
  question: string;
  handlers: ChatStreamHandlers;
  signal?: AbortSignal;
};

const DEFAULT_API_URL =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") ||
  "http://localhost:8000";

/**
 * Parse a single SSE event block (event: / data: lines) into name + JSON payload.
 */
function parseSseBlock(block: string): { event: string; data: unknown } | null {
  const lines = block.split(/\r?\n/).filter((l) => l.length > 0);
  if (lines.length === 0) return null;

  let event = "message";
  const dataLines: string[] = [];

  for (const line of lines) {
    if (line.startsWith(":")) continue; // comment / keepalive
    if (line.startsWith("event:")) {
      event = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }

  if (dataLines.length === 0) return null;

  const raw = dataLines.join("\n");
  let data: unknown = raw;
  try {
    data = JSON.parse(raw);
  } catch {
    // leave as string if not JSON
  }

  return { event, data };
}

function dispatch(event: string, data: unknown, handlers: ChatStreamHandlers) {
  switch (event) {
    case "status":
      handlers.onStatus?.(data as StatusEvent);
      break;
    case "token":
      handlers.onToken?.(data as TokenEvent);
      break;
    case "citation":
      handlers.onCitation?.(data as CitationEvent);
      break;
    case "gap":
      handlers.onGap?.(data as GapEvent);
      break;
    case "done":
      handlers.onDone?.(data as DoneEvent);
      break;
    case "error":
      handlers.onError?.(data as ErrorEvent);
      break;
    default:
      break;
  }
}

/**
 * POST a question to /chat/stream and consume text/event-stream via ReadableStream.
 * EventSource cannot POST, so we use fetch + incremental SSE parsing.
 */
export async function streamChat({
  apiUrl = DEFAULT_API_URL,
  question,
  handlers,
  signal,
}: StreamChatOptions): Promise<void> {
  const url = `${apiUrl.replace(/\/$/, "")}/chat/stream`;

  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify({ question }),
    signal,
  });

  if (!response.ok) {
    const text = await response.text().catch(() => "");
    handlers.onError?.({
      message: text || `HTTP ${response.status}`,
      recoverable: response.status >= 500 || response.status === 429,
    });
    return;
  }

  if (!response.body) {
    handlers.onError?.({
      message: "No response body from chat stream",
      recoverable: false,
    });
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    // SSE events are separated by blank lines
    let sep: number;
    while ((sep = buffer.search(/\r?\n\r?\n/)) !== -1) {
      const block = buffer.slice(0, sep);
      buffer = buffer.slice(sep).replace(/^\r?\n\r?\n/, "");

      const parsed = parseSseBlock(block);
      if (parsed) {
        dispatch(parsed.event, parsed.data, handlers);
      }
    }
  }

  // Flush trailing block without final blank line
  const trailing = buffer.trim();
  if (trailing) {
    const parsed = parseSseBlock(trailing);
    if (parsed) {
      dispatch(parsed.event, parsed.data, handlers);
    }
  }
}

export { DEFAULT_API_URL };
