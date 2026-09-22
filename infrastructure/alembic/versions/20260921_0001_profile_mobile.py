"""candidate_profiles.mobile_number — inline form pre-fill (owner decision 2026-09-21)

Revision ID: 20260921_0001
Revises: 20260913_0001
Create Date: 2026-09-21

Nullable text: mobile numbers are PII, encrypted at rest via the SEC-002
path like the other identifiers (never a separate table — one profile row).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260921_0001"
down_revision: str | None = "20260913_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE candidate_profiles ADD COLUMN mobile_number text")


def downgrade() -> None:
    op.execute("ALTER TABLE candidate_profiles DROP COLUMN mobile_number")
