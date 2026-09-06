"""eligibility evidence — P6 F-016 (SEC-009/FR-NOT-007: every match decision
persists its evidence chain: cited rows, rationale, thresholds used).

Revision ID: 20260905_0002
Revises: 20260905_0001
Create Date: 2026-09-05
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260905_0002"
down_revision: str | None = "20260905_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE eligibility_records "
        "ADD COLUMN evidence jsonb NOT NULL DEFAULT '{}'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE eligibility_records DROP COLUMN evidence")
