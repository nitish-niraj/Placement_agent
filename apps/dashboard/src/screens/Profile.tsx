import { useApi } from "../api";
import { fmt } from "../ui";
import { Card, ErrorNote, Loading, Table } from "../ui";

/** Profile / Settings / Audit — read-only cockpit views (04_UIUX_Brief §3.7-3.9). */

interface Profile {
  profile: Record<string, unknown>;
  aliases: { alias: string; kind: string }[];
}

export function Profile() {
  const { data, error, loading } = useApi<Profile>("/profile");
  if (loading) return <Loading />;
  if (error || !data) return <ErrorNote message={error ?? "no data"} />;
  const p = data.profile as Record<string, string | number | null>;
  const fields = ["canonical_name", "registration_number", "roll_number", "student_id",
                  "branch", "batch", "cgpa", "backlog_count"];
  return (
    <>
      <Card title="Candidate profile (SEC-002: sensitive fields encrypted at rest)" wide>
        <Table
          head={["Field", "Value"]}
          rows={fields.map((f) => [f, p[f] === null || p[f] === undefined ? "—" : String(p[f])])}
        />
      </Card>
      <Card title="Identity aliases (FR-PRO-002)" wide>
        <Table
          head={["Alias", "Kind"]}
          rows={data.aliases.map((a) => [a.alias, a.kind])}
        />
      </Card>
    </>
  );
}

interface Metrics {
  counters: Record<string, number>;
}

export function Settings() {
  const groups = useApi<{ groups: Record<string, unknown>[] }>("/groups");
  const metrics = useApi<Metrics>("/metrics");
  if (groups.loading || metrics.loading) return <Loading />;
  if (groups.error || !groups.data) return <ErrorNote message={groups.error ?? "no data"} />;
  return (
    <>
      <Card title="Group allowlist (FR-WA-004/005, SEC-006)" wide>
        <Table
          head={["Group", "Category", "Enabled", "Messages"]}
          rows={groups.data.groups.map((g) => [
            String(g.name), String(g.category),
            g.enabled ? "✅ allowlisted" : "—",
            String(g.message_count),
          ])}
        />
      </Card>
      <Card title="Operational counters (TRD §10.2)" wide>
        {metrics.data && Object.keys(metrics.data.counters).length > 0 ? (
          <Table
            head={["Counter", "Value"]}
            rows={Object.entries(metrics.data.counters).map(([k, v]) => [k, String(v)])}
          />
        ) : (
          <p className="empty">No counters recorded yet.</p>
        )}
      </Card>
    </>
  );
}

interface Audit {
  audit: { actor: string; action: string; entity_type: string; entity_id: string | null;
           result: string; metadata: Record<string, unknown>; created_at: string }[];
}

export function Audit() {
  const { data, error, loading } = useApi<Audit>("/audit");
  if (loading) return <Loading />;
  if (error || !data) return <ErrorNote message={error ?? "no data"} />;
  return (
    <Card title="Audit trail — every decision is traceable (SEC-004, NFR-007)" wide>
      <Table
        head={["When", "Actor", "Action", "Entity", "Result"]}
        rows={data.audit.map((a) => [
          fmt(a.created_at), a.actor, a.action,
          `${a.entity_type} ${a.entity_id ? a.entity_id.slice(0, 8) : ""}`,
          a.result,
        ])}
      />
    </Card>
  );
}
