import { useEffect, useRef, useState } from "react";
import { ask } from "./api.js";
import AnswerCard from "./components/AnswerCard.jsx";
import ChatInput from "./components/ChatInput.jsx";
import Loading from "./components/Loading.jsx";

const HISTORY_MESSAGES = 6; // last 3 turns

const EXAMPLES = [
  "Total revenue last month",
  "Which dish made us the most money last quarter?",
  "Revenue by category for the last 4 quarters",
  "How much of our business is vegetarian?",
];

// What the router sees of an earlier assistant turn.
function historyText(response) {
  if (response.type === "answer") {
    const q = response.meta?.resolved_query;
    return `${q ? `[answered: ${q}] ` : ""}${response.answer.summary}`;
  }
  return response.message || "";
}

export default function App() {
  const [messages, setMessages] = useState([]);
  const [busy, setBusy] = useState(false);
  const [stage, setStage] = useState(null);
  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, stage]);

  async function send(text) {
    const history = messages.slice(-HISTORY_MESSAGES).map((m) =>
      m.role === "user"
        ? { role: "user", content: m.text }
        : { role: "assistant", content: historyText(m.response).slice(0, 4000) }
    );
    setMessages((prev) => [...prev, { role: "user", text }]);
    setBusy(true);
    setStage({ stage: "route" });
    try {
      const response = await ask(text, history, setStage);
      setMessages((prev) => [...prev, { role: "assistant", response }]);
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          response: { type: "error", message: `Couldn't reach the server: ${err.message}`, meta: {} },
        },
      ]);
    } finally {
      setBusy(false);
      setStage(null);
    }
  }

  return (
    <div className="app">
      <header className="header">
        <h1>Alankar Restaurant</h1>
        <p>Analytics Platform</p>
      </header>

      <main className="chat">
        <div className="chat-inner">
          {messages.length === 0 && (
            <div className="empty">
              <p>Ask a question about the restaurant's menu and sales.</p>
              <div className="examples">
                {EXAMPLES.map((q) => (
                  <button key={q} disabled={busy} onClick={() => send(q)}>
                    {q}
                  </button>
                ))}
              </div>
            </div>
          )}
          {messages.map((m, i) =>
            m.role === "user" ? (
              <div key={i} className="row row-user">
                <div className="bubble">{m.text}</div>
              </div>
            ) : (
              <div key={i} className="row row-assistant">
                <AnswerCard response={m.response} />
              </div>
            )
          )}
          {busy && (
            <div className="row row-assistant">
              <Loading stage={stage} />
            </div>
          )}
          <div ref={bottomRef} />
        </div>
      </main>

      <footer className="footer">
        <ChatInput disabled={busy} onSend={send} />
      </footer>
    </div>
  );
}
