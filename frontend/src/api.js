// Streams stage events from POST /api/chat/stream (SSE over fetch), resolving with the ChatResponse.
// Falls back to the blocking POST /api/chat if the stream cannot be opened.

export async function ask(message, history, onStage) {
  const body = JSON.stringify({ message, history });
  let res;
  try {
    res = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
    });
  } catch {
    res = null;
  }
  if (!res || !res.ok || !res.body) return askBlocking(body);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result = null;

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const dataLine = frame.split("\n").find((l) => l.startsWith("data: "));
      if (!dataLine) continue;
      const evt = JSON.parse(dataLine.slice(6));
      if (evt.event === "stage") onStage?.(evt);
      else if (evt.event === "result") result = evt.data;
    }
  }
  if (!result) throw new Error("The server closed the connection before sending an answer.");
  return result;
}

async function askBlocking(body) {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
  });
  if (!res.ok) throw new Error(`Server returned ${res.status}`);
  return res.json();
}
