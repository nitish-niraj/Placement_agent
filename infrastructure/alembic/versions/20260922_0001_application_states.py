"""application_states — per company+role application tracking.

Revision ID: 20260922_0001
Revises: 20260921_0001
Create Date: 2026-09-22

Company mentioned != user applied. Eligibility records list-membership; this
table records the student's own application decision per opportunity
(company + normalized role). Status is a TEXT column with a CHECK constraint
(like companies.lifecycle_stage) so future statuses need no PG-enum dance.
Every status change is user-driven and audited by the writer (records.py).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260922_0001"
down_revision: str | None = "20260921_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE application_states (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id uuid NOT NULL REFERENCES users(id),
        company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
        role_normalized text NOT NULL DEFAULT '',
        opportunity_key text NOT NULL,
        status text NOT NULL DEFAULT 'UNKNOWN'
            CHECK (status IN ('UNKNOWN', 'ELIGIBLE_NOT_APPLIED', 'APPLIED',
                              'NOT_APPLIED', 'NOT_SURE', 'NOT_INTERESTED')),
        applied_at timestamptz,
        source text NOT NULL DEFAULT '',
        note text NOT NULL DEFAULT '',
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (user_id, company_id, role_normalized)
    )"""
    )
    op.execute(
        "CREATE INDEX idx_application_states_opportunity "
        "ON application_states (opportunity_key)"
    )
    op.execute(
        "CREATE INDEX idx_application_states_company "
        "ON application_states (company_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE application_states")
