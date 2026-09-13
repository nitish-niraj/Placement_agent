import { useState } from "react";
import { post, useApi } from "../api";
import { fmt } from "../ui";
import { Badge, Card, ErrorNote, Loading, Table } from "../ui";

/** Approvals (P13/F-030) — nothing reaches the real world without an
 * explicit owner decision (ADR-008). Pre-fill comes from the stored profile
 * at read time; the submission executor itself lands in P14. */

interface ActionRow {
  id: string;
  type: string;
  target: string;
  status: string;
  risk_level: string;
  approval_required: boolean;
  created_at: string;
  updated_at: string;
  event: { id: string; title: string | null; company: string | null } | null;
  payload: { presenters?: string[]; company?: string | null; source?: string;
             reason?: string; proposal_type?: string;
             correction?: { event_id?: string; field?: string; value?: string };
             reparse_message_id?: string } | null;
  prefill: Record<string, string | number | null> | null;
}

interface ActionsResponse {
  actions: ActionRow[];
}

const PREFILL_LABELS: Record<string, string> = {
  full_name: "Full name", email: "Email", roll_number: "Roll no.",
  registration_number: "Registration no.", student_id: "Student ID",
  branch: "Branch", batch: "Batch", cgpa: "CGPA",
  tenth_percent: "10th %", twelfth_percent: "12th %", backlog_count: "Backlogs",
};

/** Stage 3 (ADR-013): what the executor will do the moment you approve. */
function willDo(a: ActionRow): string {
  const correction = a.payload?.correction as
    | { event_id?: string; field?: string; value?: string }
    | undefined;
  switch (a.type) {
    case "deadline_nudge":
    case "kyc_reminder":
    case "follow_up":
      return "send you a Telegram reminder card now";
    case "verify_field":
      return correction
        ? `set "${correction.field}" = "${correction.value}" on event ${(correction.event_id ?? "").slice(0, 8)}`
        : "apply the field correction";
    case "data_quality":
      return `re-run the event parser on message ${(a.payload?.reparse_message_id ?? "").slice(0, 8)}`;
    case "form_draft":
      return a.payload && (a.payload as { source?: string }).source === "teams_meeting_chat"
        ? "submit this form (feedback form)"
        : "submit this form after your approval";
    default:
      return "run the approved executor";
  }
}

function PrefillDetails({ prefill }: { prefill: Record<string, string | number | null> }) {
  return (
    <details>
      <summary>pre-filled from your profile</summary>
      <table className="prefill">
        <tbody>
          {Object.entries(prefill).map(([key, value]) => (
            <tr key={key}>
              <td>{PREFILL_LABELS[key] ?? key}</td>
              <td>{value === null || value === "" ? "—" : String(value)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}

export function Approvals() {
  const { data, error, loading, reload } = useApi<ActionsResponse>("/actions");
  const [busy, setBusy] = useState<string | null>(null);

  if (loading) return <Loading />;
  if (error || !data) return <ErrorNote message={error ?? "no data"} />;

  const pending = data.actions.filter((a) => a.status === "WAITING_APPROVAL");
  const decided = data.actions.filter((a) => a.status !== "WAITING_APPROVAL").slice(0, 15);

  const decide = async (id: string, decision: "approve" | "reject") => {
    setBusy(id + decision);
    try {
      await post(`/actions/${id}/${decision}`, {});
      reload();
    } finally {
      setBusy(null);
    }
  };

  return (
    <>
      <Card title="Awaiting your approval — nothing submits without you (ADR-008)" wide>
        <Table
          head={["Proposed", "Form", "Pre-fill", "Actions"]}
          rows={pending.map((a) => [
            fmt(a.created_at),
            <span key="t">
              <span className="badge">{a.type}</span>{" "}
              {a.event?.company ?? a.payload?.company ?? "General"} —{" "}
              {a.target.startsWith("http")
                ? <a href={a.target} target="_blank" rel="noreferrer">{a.target}</a>
                : <code>{a.target}</code>}
              {a.payload?.reason ? (
                <><br /><span className="muted">💡 {a.payload.reason}</span></>
              ) : null}
              {a.payload?.presenters?.length ? (
                <><br /><span className="muted">👤 Teacher/presenter: {a.payload.presenters.join(", ")}</span></>
              ) : null}
              {a.event?.title ? <><br /><span className="muted">{a.event.title}</span></> : null}
              <><br /><span className="muted">✓ approving will: {willDo(a)}</span></>
            </span>,
            a.prefill ? <PrefillDetails key="p" prefill={a.prefill} /> : "—",
            <span key="a" className="actions">
              <button disabled={busy === a.id + "approve"}
                      onClick={() => decide(a.id, "approve")}>✓ Approve</button>
              <button className="secondary" disabled={busy === a.id + "reject"}
                      onClick={() => decide(a.id, "reject")}>✗ Reject</button>
            </span>,
          ])}
        />
        {pending.length === 0 && <p className="muted">No drafts awaiting approval.</p>}
      </Card>

      <Card title="Recent decisions" wide>
        <Table
          head={["Updated", "Type", "Form", "Status"]}
          rows={decided.map((a) => [
            fmt(a.updated_at),
            a.type,
            <a key="t" href={a.target} target="_blank" rel="noreferrer">
              {a.target.length > 60 ? a.target.slice(0, 60) + "…" : a.target}
            </a>,
            <Badge key="s" kind="action" value={a.status} />,
          ])}
        />
        {decided.length === 0 && <p className="muted">No decided drafts yet.</p>}
        <p className="muted">
          Approving hands the draft to its executor — forms submit via the
          Stage 3 robot, reviewer proposals run their approved action. Every
          run is audited.
        </p>
      </Card>
    </>
  );
}
