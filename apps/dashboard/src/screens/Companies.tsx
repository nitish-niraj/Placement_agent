import { Link, useParams } from "react-router-dom";
import { useApi } from "../api";
import { fmt } from "../ui";
import { Badge, Card, ErrorNote, Loading, stripHtml, Table } from "../ui";

/** Companies + F-027 company timeline: original + updates + evidence. */

interface Companies {
  companies: {
    id: string; canonical_name: string; watch_state: string;
    lifecycle_stage: string | null; first_seen_at: string | null;
    event_count: number; eligibility_state: string | null;
  }[];
}

export function Companies() {
  const { data, error, loading } = useApi<Companies>("/companies");
  if (loading) return <Loading />;
  if (error || !data) return <ErrorNote message={error ?? "no data"} />;
  return (
    <Card title="Companies — memory & watch state (FR-MEM-001..005)" wide>
      <Table
        head={["Company", "Watch", "Lifecycle", "Eligibility", "Events", "First seen"]}
        rows={data.companies.map((c) => [
          <Link key="l" to={`/companies/${c.id}`}>{c.canonical_name}</Link>,
          <Badge key="w" kind="watch" value={c.watch_state} />,
          c.lifecycle_stage ?? "—",
          <Badge key="e" kind="elig" value={c.eligibility_state} />,
          c.event_count,
          fmt(c.first_seen_at, false),
        ])}
      />
    </Card>
  );
}

interface Timeline {
  company: { canonical_name: string; watch_state: string; lifecycle_stage: string | null };
  timeline: {
    kind: string; at: string; type?: string; status?: string; title?: string;
    deadline_at?: string | null; start_at?: string | null; state?: string;
    match_method?: string | null; confidence?: string | null; reason?: string | null; update_count?: number;
    priority?: string; delta?: Record<string, { from: unknown; to: unknown }>;
    current_payload?: { excerpt?: string; venue?: string; links?: string[] };
    evidence?: { refs?: { location?: string; quote?: string }[] };
  }[];
}

const KIND_LABEL: Record<string, string> = {
  event: "📅 Event", update: "✏️ Update", eligibility: "🎓 Eligibility",
  notification: "🔔 Notification",
};

export function CompanyTimeline() {
  const { id } = useParams();
  const { data, error, loading } = useApi<Timeline>(`/companies/${id}/timeline`);
  if (loading) return <Loading />;
  if (error || !data) return <ErrorNote message={error ?? "no data"} />;
  return (
    <>
      <Card title={`${data.company.canonical_name} — timeline (FR-DED-005)`} wide>
        <p>
          <Badge kind="watch" value={data.company.watch_state} />{" "}
          Lifecycle: {data.company.lifecycle_stage ?? "—"}
        </p>
        {data.timeline.map((item, i) => (
          <div key={i} className={`timeline-item timeline-${item.kind}`}>
            <span className="timeline-kind">{KIND_LABEL[item.kind] ?? item.kind}</span>
            <span className="timeline-at">{fmt(item.at)}</span>
            <div className="timeline-body">
              {item.kind === "event" && (
                <>
                  <b>{item.title ?? item.type}</b>{" "}
                  <Badge kind="event" value={item.status ?? null} />
                  {item.deadline_at && <div>Deadline: {fmt(item.deadline_at)}</div>}
                  {item.start_at && <div>Starts: {fmt(item.start_at)}</div>}
                  {item.current_payload?.venue && <div>Venue: {item.current_payload.venue}</div>}
                  {item.update_count ? <div className="muted">{item.update_count} update(s) below</div> : null}
                  {item.current_payload?.excerpt &&
                    <div className="muted">“{stripHtml(item.current_payload.excerpt, 160)}”</div>}
                </>
              )}
              {item.kind === "update" && item.delta && (
                <ul>
                  {Object.entries(item.delta).map(([field, change]) => (
                    <li key={field}><b>{field}</b>: {String(change.from) || "—"} → {String(change.to)}</li>
                  ))}
                </ul>
              )}
              {item.kind === "eligibility" && (
                <>
                  <Badge kind="elig" value={item.state ?? null} /> via {item.match_method ?? "—"}
                  {item.confidence && ` (confidence ${item.confidence})`}
                  {item.evidence?.refs?.[0]?.location &&
                    <div className="muted">Evidence: {item.evidence.refs[0].location}
                      {item.evidence.refs[0].quote ? ` — “${item.evidence.refs[0].quote}”` : ""}</div>}
                </>
              )}
              {item.kind === "notification" && (
                <>{item.priority} · <Badge kind="notif" value={item.status ?? null} />
                  {item.reason ? <div className="muted">{item.reason}</div> : null}</>
              )}
            </div>
          </div>
        ))}
      </Card>
      <p><Link to="/companies">← All companies</Link></p>
    </>
  );
}

/** Global timeline: all canonical events (master §17 "Timeline" screen). */
export function Timeline() {
  const { data, error, loading } = useApi<{ events: Record<string, string | null>[] }>("/events");
  if (loading) return <Loading />;
  if (error || !data) return <ErrorNote message={error ?? "no data"} />;
  return (
    <Card title="Timeline — all canonical events" wide>
      <Table
        head={["Created", "Company", "Type", "Status", "Deadline", "Venue"]}
        rows={data.events.map((e) => [
          fmt(e.created_at), e.company ?? "General", e.type,
          <Badge key="s" kind="event" value={e.status} />,
          fmt(e.deadline_at), e.venue ?? "—",
        ])}
      />
    </Card>
  );
}
