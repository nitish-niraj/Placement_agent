import type { ReactNode } from "react";
import { useEffect, useState } from "react";

/** Shared cockpit UI primitives — calm by default (04_UIUX_Brief §1). */

export function Card({ title, children, wide = false }: {
  title: ReactNode; children: ReactNode; wide?: boolean;
}) {
  return (
    <section className={`card ${wide ? "card-wide" : ""}`}>
      <h2>{title}</h2>
      {children}
    </section>
  );
}

export function Badge({ kind, value }: { kind: string; value: string | null }) {
  if (!value) return <span className="badge muted">—</span>;
  return <span className={`badge badge-${kind} badge-${value}`}>{value}</span>;
}

export function Table({ head, rows }: { head: string[]; rows: ReactNode[][] }) {
  if (rows.length === 0) return <p className="empty">Nothing here yet.</p>;
  return (
    <table>
      <thead><tr>{head.map((h) => <th key={h}>{h}</th>)}</tr></thead>
      <tbody>
        {rows.map((row, i) => (
          <tr key={i}>{row.map((cell, j) => <td key={j}>{cell}</td>)}</tr>
        ))}
      </tbody>
    </table>
  );
}

export function Loading() {
  return <p className="empty">Loading…</p>;
}

export function ErrorNote({ message }: { message: string }) {
  return <p className="error-note">Failed to load: {message}</p>;
}

export function fmt(value: string | null, withTime = true): string {
  if (!value) return "—";
  const d = new Date(value);
  return d.toLocaleString("en-IN", {
    day: "2-digit", month: "short",
    ...(withTime ? { hour: "2-digit", minute: "2-digit" } : {}),
  });
}

/** Stale-connection dot (04_UIUX_Brief §7): silence is detected, not assumed. */
export function useHealth() {
  const [health, setHealth] = useState<{ status: string; checks: Record<string, string> } | null>(null);
  useEffect(() => {
    const load = () =>
      fetch("/health")
        .then((r) => r.json())
        .then(setHealth)
        .catch(() => setHealth(null));
    load();
    const t = setInterval(load, 30_000);
    return () => clearInterval(t);
  }, []);
  return health;
}

export function stripHtml(text: string | null, max = 140): string {
  if (!text) return "—";
  const plain = text.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
  return plain.length > max ? plain.slice(0, max) + "…" : plain;
}
