import { useState } from "react";
import { post, useApi } from "../api";
import { Badge, Card, ErrorNote, Loading, Table, fmt } from "../ui";

/** Application states (DEC-011) — company mentioned != applied. Your answer
 * per company+role drives follow-ups: post-application updates only become
 * follow-ups for APPLIED rows. Mirrors the Telegram answer buttons. */

interface ApplicationRow {
  id: string;
  company: string;
  role: string;
  opportunity_key: string;
  status: string;
  applied_at: string | null;
  source: string;
  note: string;
  updated_at: string;
}

interface ApplicationsResponse {
  applications: ApplicationRow[];
}

const STATUSES = [
  ["APPLIED", "✅ Applied"],
  ["NOT_APPLIED", "❌ Not applied"],
  ["NOT_SURE", "🤔 Not sure"],
  ["NOT_INTERESTED", "🚫 Not interested"],
] as const;

export function Applications() {
  const { data, error, loading, reload } = useApi<ApplicationsResponse>("/applications");
  const [busy, setBusy] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);

  if (loading) return <Loading />;
  if (error || !data) return <ErrorNote message={error ?? "no data"} />;

  const answer = async (row: ApplicationRow, status: string) => {
    setBusy(row.id + status);
    setFailed(null);
    try {
      await post("/applications/answer", {
        company: row.company, role: row.role || null, status,
      });
      reload();
    } catch (e) {
      setFailed(e instanceof Error ? e.message : "answer failed");
    } finally {
      setBusy(null);
    }
  };

  const undecided = data.applications.filter((a) =>
    ["UNKNOWN", "NOT_SURE", "ELIGIBLE_NOT_APPLIED"].includes(a.status));
  const decided = data.applications.filter((a) =>
    !["UNKNOWN", "NOT_SURE", "ELIGIBLE_NOT_APPLIED"].includes(a.status));

  const table = (rows: ApplicationRow[]) => (
    <Table
      head={["Company", "Role", "Status", "Updated", "Your answer"]}
      rows={rows.map((a) => [
        a.company,
        a.role || "—",
        <span key="s"><Badge kind="application" value={a.status} />{a.source ? (
          <><br /><span className="muted">via {a.source}</span></>
        ) : null}</span>,
        fmt(a.updated_at),
        <span key="a" className="actions">
          {STATUSES.map(([value, label]) => (
            <button key={value} className="secondary"
                    disabled={busy === a.id + value || a.status === value}
                    onClick={() => answer(a, value)}>
              {busy === a.id + value ? "…" : label}
            </button>
          ))}
        </span>,
      ])}
    />
  );

  return (
    <>
      <Card title="Awaiting your answer — follow-ups need it" wide>
        {undecided.length === 0
          ? <p className="muted">Nothing undecided. Every tracked opportunity has an answer.</p>
          : table(undecided)}
        {failed && <ErrorNote message={failed} />}
      </Card>
      <Card title="Decided" wide>
        {decided.length === 0
          ? <p className="muted">No answers recorded yet.</p>
          : table(decided)}
        <p className="muted">
          A company mention alone never counts as an application — only your
          answer here (or the Telegram buttons) moves these states. Changing
          an answer re-arms future follow-ups immediately.
        </p>
      </Card>
    </>
  );
}
