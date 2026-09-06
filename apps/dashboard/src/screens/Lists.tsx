import { useState } from "react";
import { useApi } from "../api";
import { fmt } from "../ui";
import { Badge, Card, ErrorNote, Loading, stripHtml, Table } from "../ui";

/** Inbox / Documents / Notifications / Eligibility — evidence-backed tables. */

export function Inbox() {
  const [onlyImportant, setOnlyImportant] = useState(true);
  const { data, error, loading } = useApi<{ messages: Record<string, string | null>[] }>(
    `/messages?important_only=${onlyImportant}`, [onlyImportant]);
  if (loading) return <Loading />;
  if (error || !data) return <ErrorNote message={error ?? "no data"} />;
  return (
    <Card
      title={
        <>
          Inbox — AI-filtered important messages{" "}
          <label className="inline">
            <input type="checkbox" checked={onlyImportant}
                   onChange={(e) => setOnlyImportant(e.target.checked)} /> important only
          </label>
        </>
      }
      wide
    >
      <Table
        head={["Sent", "Group", "Domain", "Importance", "Message"]}
        rows={data.messages.map((m) => [
          fmt(m.sent_at), m.group_name, m.domain ?? "—",
          <Badge key="i" kind="importance" value={m.importance} />,
          stripHtml(m.text, 180),
        ])}
      />
    </Card>
  );
}

export function Documents() {
  const { data, error, loading } = useApi<{ data: Record<string, unknown>[] }>("/documents");
  if (loading) return <Loading />;
  if (error || !data) return <ErrorNote message={error ?? "no data"} />;
  return (
    <Card title="Documents — parsed candidate lists with evidence" wide>
      <Table
        head={["Parsed", "File", "Extractor", "Company detected", "Rows", "Review"]}
        rows={data.data.map((d) => [
          fmt(String(d.sent_at ?? "")),
          String(d.file_name ?? "—"),
          String(d.extractor),
          String((d.detection as { company?: string } | null)?.company ?? "—"),
          String(d.row_count),
          d.needs_review ? <span className="badge badge-deadline-DUE_SOON">needs review</span> : "—",
        ])}
      />
    </Card>
  );
}

export function Notifications() {
  const { data, error, loading } = useApi<{ notifications: Record<string, string | null>[] }>(
    "/notifications");
  if (loading) return <Loading />;
  if (error || !data) return <ErrorNote message={error ?? "no data"} />;
  return (
    <Card title="Notifications — delivered and suppressed with reasons (FR-NOT-007)" wide>
      <Table
        head={["Created", "Priority", "Status", "Reason", "Delivered"]}
        rows={data.notifications.map((n) => [
          fmt(n.created_at),
          <Badge key="p" kind="importance" value={n.priority} />,
          <Badge key="s" kind="notif" value={n.status} />,
          stripHtml(n.reason, 90),
          fmt(n.sent_at),
        ])}
      />
    </Card>
  );
}

export function Eligibility() {
  const { data, error, loading } = useApi<{ eligibility: Record<string, unknown>[] }>("/eligibility");
  if (loading) return <Loading />;
  if (error || !data) return <ErrorNote message={error ?? "no data"} />;
  return (
    <Card title="Eligibility records — every match decision with evidence (§10.2)" wide>
      <Table
        head={["Detected", "Company", "State", "Match", "Confidence", "Evidence", "Review"]}
        rows={data.eligibility.map((r) => {
          const evidence = (r.evidence as { refs?: { location?: string; quote?: string }[] })
            ?.refs ?? [];
          return [
            fmt(String(r.detected_at)),
            String(r.company),
            <Badge key="s" kind="elig" value={String(r.state)} />,
            String(r.match_method ?? "—"),
            String(r.confidence ?? "—"),
            evidence.length
              ? `${evidence[0].location ?? ""} — “${evidence[0].quote ?? ""}”`
              : "—",
            r.requires_user_review ? "⚠ needs review" : "—",
          ];
        })}
      />
    </Card>
  );
}
