import { useState } from "react";
import { post, useApi } from "../api";
import { fmt } from "../ui";
import { Badge, Card, ErrorNote, Loading, Table } from "../ui";

/** Overview — personal cockpit (master §17): what matters to me today. */

interface Overview {
  counts: Record<string, number>;
  needs_review: {
    id: string; state: string; match_method: string | null; confidence: string | null;
    reason: string | null; company: string;
  }[];
  deadlines: { id: string; due_at: string; state: string; type: string; company: string | null }[];
}

export function Overview() {
  const { data, error, loading, reload } = useApi<Overview>("/overview");
  const [busy, setBusy] = useState<string | null>(null);

  if (loading) return <Loading />;
  if (error || !data) return <ErrorNote message={error ?? "no data"} />;

  const correct = async (id: string, decision: string) => {
    setBusy(id + decision);
    try {
      await post("/feedback/match", { eligibility_record_id: id, correction: decision,
                                     note: "dashboard correction" });
      reload();
    } finally {
      setBusy(null);
    }
  };

  return (
    <>
      <div className="cards-row">
        <Card title="Eligible companies"><span className="big">{data.counts.eligible_companies}</span></Card>
        <Card title="Active events"><span className="big">{data.counts.events_active}</span></Card>
        <Card title="Open deadlines"><span className="big">{data.counts.deadlines_open}</span></Card>
        <Card title="Alerts (24h)"><span className="big">{data.counts.notifications_24h}</span></Card>
        <Card title="Pending delivery"><span className="big">{data.counts.pending_delivery}</span></Card>
      </div>

      <Card title="Needs your review — ambiguous matches never auto-confirm (ADR-005)" wide>
        <Table
          head={["Company", "State", "Method", "Confidence", "Actions"]}
          rows={data.needs_review.map((r) => [
            r.company,
            <Badge key="s" kind="elig" value={r.state} />,
            r.match_method ?? "—",
            r.confidence ?? "—",
            <span key="a" className="actions">
              <button disabled={busy === r.id + "confirm"}
                      onClick={() => correct(r.id, "confirm")}>✓ Confirm</button>
              <button className="secondary" disabled={busy === r.id + "deny"}
                      onClick={() => correct(r.id, "deny")}>✗ Deny</button>
            </span>,
          ])}
        />
      </Card>

      <Card title="Upcoming deadlines" wide>
        <Table
          head={["Due", "State", "Type", "Company"]}
          rows={data.deadlines.map((d) => [
            fmt(d.due_at),
            <Badge key="d" kind="deadline" value={d.state} />,
            d.type,
            d.company ?? "General",
          ])}
        />
      </Card>
    </>
  );
}
