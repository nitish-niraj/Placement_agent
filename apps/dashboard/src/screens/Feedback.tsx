import { useState } from "react";
import { post, useApi } from "../api";
import { Card, ErrorNote, Loading, Table, fmt } from "../ui";

/** Classifier feedback intake — corrections the rules engine learns from.
 * Stored predicted-vs-correct with audit; history is never rewritten and
 * corpus promotion stays manual curation. */

interface FeedbackRow {
  id: string;
  message_id: string;
  predicted_domain: string | null;
  predicted_importance: string | null;
  correct_domain: string;
  correct_importance: string | null;
  note: string;
  excerpt: string | null;
  created_at: string;
}

interface FeedbackResponse {
  feedback: FeedbackRow[];
}

const DOMAINS = ["PLACEMENT", "ACADEMIC", "EXAMINATION", "ADMINISTRATIVE",
  "EVENT", "GENERAL", "UNKNOWN"];
const IMPORTANCES = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "IGNORE"];

export function Feedback() {
  const { data, error, loading, reload } = useApi<FeedbackResponse>(
    "/feedback/classification");
  const [messageId, setMessageId] = useState("");
  const [domain, setDomain] = useState("PLACEMENT");
  const [importance, setImportance] = useState("MEDIUM");
  const [note, setNote] = useState("");
  const [failed, setFailed] = useState<string | null>(null);
  const [sending, setSending] = useState(false);

  const submit = async () => {
    if (!messageId.trim()) { setFailed("paste a message id first"); return; }
    setSending(true);
    setFailed(null);
    try {
      await post("/feedback/classification", {
        message_id: messageId.trim(),
        correct_domain: domain,
        correct_importance: importance,
        note: note.trim(),
      });
      setMessageId("");
      setNote("");
      reload();
    } catch (e) {
      setFailed(e instanceof Error ? e.message : "submit failed");
    } finally {
      setSending(false);
    }
  };

  return (
    <>
      <Card title="Correct a classification">
        <p className="muted">
          Got a wrong category or priority? Point at the message and set it
          right — the correction is stored with what the pipeline predicted,
          for manual corpus curation. Nothing already sent is rewritten.
        </p>
        <label>Message id<br />
          <input placeholder="uuid from the inbox / digest debug"
                 value={messageId} onChange={(e) => setMessageId(e.target.value)} />
        </label>
        <div className="actions">
          <label>Correct domain<br />
            <select value={domain} onChange={(e) => setDomain(e.target.value)}>
              {DOMAINS.map((d) => <option key={d} value={d}>{d}</option>)}
            </select>
          </label>
          <label>Correct importance<br />
            <select value={importance}
                    onChange={(e) => setImportance(e.target.value)}>
              {IMPORTANCES.map((d) => <option key={d} value={d}>{d}</option>)}
            </select>
          </label>
        </div>
        <label>Note (optional)<br />
          <input placeholder="why is this wrong?"
                 value={note} onChange={(e) => setNote(e.target.value)} />
        </label>
        <div className="actions">
          <button disabled={sending} onClick={submit}>
            {sending ? "…" : "✓ Record correction"}
          </button>
        </div>
        {failed && <ErrorNote message={failed} />}
      </Card>
      <Card title="Intake queue (newest first)" wide>
        {loading ? <Loading /> : null}
        {error || !data ? <ErrorNote message={error ?? "no data"} /> : (
          <Table
            head={["When", "Message", "Predicted", "Corrected", "Note"]}
            rows={data.feedback.slice(0, 30).map((f) => [
              fmt(f.created_at),
              <span key="m" title={f.message_id}>
                {(f.excerpt ?? f.message_id).slice(0, 80)}
              </span>,
              `${f.predicted_domain ?? "—"} / ${f.predicted_importance ?? "—"}`,
              `${f.correct_domain} / ${f.correct_importance ?? "—"}`,
              f.note || "—",
            ])}
          />
        )}
      </Card>
    </>
  );
}
