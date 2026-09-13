import { useState } from "react";
import { api } from "../api";
import { Card, Loading } from "../ui";

/** Ask PIA (F-028, P12) — conversational search, source-backed answers only. */

interface AskResult {
  answer: string;
  citations: { kind: string; ref: string; quote: string | null }[];
  says_unavailable: boolean;
  confidence: number;
  fallback: boolean;
  /** Which rung answered: nim | openrouter | groq | deterministic | agent | p12_fallback. */
  source?: string;
  /** ADR-011 Stage 1: per-step reasoning trace (agent runs only). */
  steps?: { step: number; thought: string; tool: string | null;
            args: Record<string, string>; observation_chars?: number }[];
  agent_degraded?: boolean;
}

export function Ask() {
  const [question, setQuestion] = useState("");
  const [deep, setDeep] = useState(false);
  const [result, setResult] = useState<AskResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const ask = async () => {
    if (question.trim().length < 3) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      setResult(await api<AskResult>(deep ? "/agent/ask" : "/ask", {
        method: "POST",
        body: JSON.stringify({ question: question.trim() }),
      }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title="Ask PIA — answers come only from your stored data (F-028)" wide>
      <div className="ask-row">
        <input
          value={question}
          placeholder="e.g. which companies am I eligible for? · what is the TECHADEMY package?"
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && ask()}
          disabled={busy}
        />
        <button onClick={ask} disabled={busy || question.trim().length < 3}>Ask</button>
      </div>
      <label className="muted" style={{ display: "flex", gap: 6, alignItems: "center" }}>
        <input type="checkbox" checked={deep}
               onChange={(e) => setDeep(e.target.checked)} disabled={busy} />
        🧠 Deep (multi-step agent — slower, shows its reasoning trace)
      </label>

      {busy && <Loading />}
      {error && <p className="error-note">{error}</p>}

      {result && (
        <div className="answer">
          <p className="answer-text">{result.answer}</p>
          {result.says_unavailable &&
            <p className="muted">The stored data doesn't cover this yet.</p>}
          {result.fallback &&
            <p className="muted">Language model unavailable — deterministic evidence below.</p>}
          {(result.source === "openrouter" || result.source === "groq") &&
            <p className="muted">answered via {result.source}</p>}
          {result.source === "p12_fallback" &&
            <p className="muted">agent degraded to the single-shot ladder</p>}
          {result.steps && result.steps.length > 0 && (
            <details>
              <summary className="muted">reasoning trace ({result.steps.length} step{result.steps.length > 1 ? "s" : ""})</summary>
              {result.steps.map((s) => (
                <p key={s.step} className="muted">
                  <strong>[{s.step}]</strong> {s.tool
                    ? <>🛠 {s.tool}({JSON.stringify(s.args)})</>
                    : "💡 final answer"} — {s.thought}
                </p>
              ))}
            </details>
          )}
          <p className="muted">confidence {Math.round(result.confidence * 100)}%</p>
          {result.citations.length > 0 && (
            <>
              <h2>Sources</h2>
              <ul className="citations">
                {result.citations.map((c, i) => (
                  <li key={i}>
                    <span className="badge">{c.kind}</span>{" "}
                    <code>{c.ref.slice(0, 8)}</code>
                    {c.quote ? <span> — “{c.quote}”</span> : null}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </Card>
  );
}
