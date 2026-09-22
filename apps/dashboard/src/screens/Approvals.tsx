import { useState } from "react";
import { api, parsePreviewError, post, useApi } from "../api";
import { buildFillSnippet } from "../fillSnippet";
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

interface PrefillPreview {
  prefill_url: string;
  source_url: string;
  filled: { question: string }[];
  left_blank: { question: string; reason: string }[];
  note: string;
}

const PREFILL_LABELS: Record<string, string> = {
  full_name: "Full name", email: "Email", mobile: "Mobile", roll_number: "Roll no.",
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
      return "open a pre-filled form below for you to review and Submit";
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

function FillKit({ prefill }: { prefill: Record<string, string | number | null> }) {
  const [copied, setCopied] = useState<string | null>(null);
  const rows = Object.entries(prefill).filter(
    ([, v]) => v !== null && v !== undefined && String(v).trim());
  if (rows.length === 0) return null;
  const copy = async (key: string, value: string) => {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = value;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    setCopied(key);
    setTimeout(() => setCopied((cur) => (cur === key ? null : cur)), 1500);
  };
  return (
    <div>
      <span className="muted">Fill kit — copy a value, paste it into the form:</span>
      <ul>
        {rows.map(([key, value]) => (
          <li key={key}>
            {PREFILL_LABELS[key] ?? key}: <code>{String(value)}</code>{" "}
            <button className="secondary"
                    onClick={() => copy(key, String(value))}>
              {copied === key ? "Copied ✓" : "Copy"}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function Approvals() {
  const { data, error, loading, reload } = useApi<ActionsResponse>("/actions");
  const [busy, setBusy] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [preview, setPreview] = useState<PrefillPreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<{ status: number | null;
                                                     message: string } | null>(null);

  if (loading) return <Loading />;
  if (error || !data) return <ErrorNote message={error ?? "no data"} />;

  const pending = data.actions.filter((a) => a.status === "WAITING_APPROVAL");
  const decided = data.actions.filter((a) => a.status !== "WAITING_APPROVAL").slice(0, 15);

  const decide = async (id: string, decision: "approve" | "reject") => {
    setBusy(id + decision);
    try {
      await post(`/actions/${id}/${decision}`, {});
      if (openId === id) setOpenId(null);
      reload();
    } finally {
      setBusy(null);
    }
  };

  const togglePreview = async (id: string) => {
    if (openId === id) { setOpenId(null); return; }
    setOpenId(id);
    setPreview(null);
    setPreviewError(null);
    setPreviewLoading(true);
    try {
      setPreview(await api<PrefillPreview>(`/actions/${id}/prefill-url`));
    } catch (e) {
      setPreviewError(parsePreviewError(e));
    } finally {
      setPreviewLoading(false);
    }
  };

  const openDraft = openId ? data.actions.find((a) => a.id === openId) ?? null : null;

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
              {a.type === "form_draft" && (
                <button className="secondary" disabled={previewLoading && openId === a.id}
                        onClick={() => togglePreview(a.id)}>
                  {openId === a.id ? "▲ Hide form" : "📝 Fill & preview"}
                </button>
              )}
            </span>,
          ])}
        />
        {pending.length === 0 && <p className="muted">No drafts awaiting approval.</p>}
      </Card>

      <Card title="Recent decisions" wide>
        <Table
          head={["Updated", "Type", "Form", "Status", "Preview"]}
          rows={decided.map((a) => [
            fmt(a.updated_at),
            a.type,
            <a key="t" href={a.target} target="_blank" rel="noreferrer">
              {a.target.length > 60 ? a.target.slice(0, 60) + "…" : a.target}
            </a>,
            <Badge key="s" kind="action" value={a.status} />,
            a.type === "form_draft" ? (
              <button key="v" className="secondary"
                      disabled={previewLoading && openId === a.id}
                      onClick={() => togglePreview(a.id)}>
                {openId === a.id ? "▲ Hide form" : "📝 Fill & preview"}
              </button>
            ) : "—",
          ])}
        />
        {decided.length === 0 && <p className="muted">No decided drafts yet.</p>}
        <p className="muted">
          Approving records your decision (audited). Forms are never submitted
          by the portal — you click Submit inside the pre-filled form above.
          Reviewer proposals run their approved action via the Stage 3 executor.
        </p>
      </Card>

      {openDraft && (
        <Card title={`Pre-filled form — ${openDraft.event?.company ?? openDraft.payload?.company ?? "General"}`} wide>
          {(() => {
            if (previewLoading) return <Loading />;
            if (previewError) {
              const notAForm = previewError.status === 422
                && /teams meeting|not a form/i.test(previewError.message);
              if (notAForm) {
                return (
                  <>
                    <p>
                      This link opens a <b>Teams meeting</b>, not a form — there
                      is nothing to fill. It was filed as a draft by mistake.
                    </p>
                    <button className="secondary"
                            disabled={busy === openDraft.id + "reject"}
                            onClick={() => decide(openDraft.id, "reject")}>
                      ✗ Dismiss draft
                    </button>
                  </>
                );
              }
              return (
                <>
                  <ErrorNote message={previewError.message} />
                  <p className="muted">
                    <a href={openDraft.target} target="_blank" rel="noreferrer">
                      Open the original link
                    </a>{" · "}
                    <button className="secondary"
                            disabled={busy === openDraft.id + "reject"}
                            onClick={() => decide(openDraft.id, "reject")}>
                      ✗ Dismiss draft
                    </button>
                  </p>
                </>
              );
            }
            if (!preview) return null;
            const prefilled = preview.filled.length > 0;
            return (
              <>
                {prefilled && (
                  <p className="muted">
                    Review it, answer the blanks, then click Submit inside the form.
                  </p>
                )}
                {!prefilled && (
                  <>
                    {preview.note && <p className="muted">{preview.note}</p>}
                    {openDraft.prefill && (
                      <FillKit prefill={openDraft.prefill} />
                    )}
                    {openDraft.prefill && (
                      <p>
                        🔖{" "}
                        <a href={buildFillSnippet(openDraft.prefill)}
                           title="Drag this link to your bookmarks bar">
                          PIA auto-fill (drag to bookmarks bar)
                        </a>
                        <br />
                        <span className="muted">
                          The portal cannot read this form (sign-in wall): open it
                          below, click the bookmark on the page, and it fills from
                          your profile. File uploads and judgement answers stay manual.
                        </span>
                      </p>
                    )}
                  </>
                )}
                <div className="prefill-lists">
                  <div>
                    <span className="muted">✓ Pre-filled ({preview.filled.length})</span>
                    <ul>{preview.filled.map((f) => <li key={f.question}>{f.question}</li>)}</ul>
                  </div>
                  <div>
                    <span className="muted">✎ Answer yourself ({preview.left_blank.length})</span>
                    <ul>{preview.left_blank.map((b) => (
                      <li key={b.question}>{b.question} — {b.reason}</li>))}</ul>
                  </div>
                </div>
                <iframe src={preview.prefill_url} title="Pre-filled Google Form"
                        className="prefill-frame" />
                <p className="muted">
                  <a href={preview.prefill_url} target="_blank" rel="noreferrer">
                    ↗ Open in new tab
                  </a>
                </p>
              </>
            );
          })()}
        </Card>
      )}
    </>
  );
}
