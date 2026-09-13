"""agent_traces — ADR-011 Stage 1 (tool-using answer agent)

Revision ID: 20260913_0001
Revises: 20260906_0001
Create Date: 2026-09-13

One row per ask_agent() call (agent answer OR P12 degradation fallback):
question, which rung answered, the answer, citations, and the full ReAct
step trace (thought/tool/args/observation size per step). Observability
only — nothing here is authoritative for eligibility/deadlines (ADR-003).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260913_0001"
down_revision: str | None = "20260906_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""CREATE TABLE agent_traces (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        question text NOT NULL,
        source text NOT NULL,
        answer text NOT NULL,
        citations jsonb NOT NULL DEFAULT '[]'::jsonb,
        steps jsonb NOT NULL DEFAULT '[]'::jsonb,
        created_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("CREATE INDEX ix_agent_traces_created_at ON agent_traces (created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE agent_traces")
