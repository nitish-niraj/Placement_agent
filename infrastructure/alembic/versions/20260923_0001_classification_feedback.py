"""classification_feedback — human corrections for the rules classifier.

Revision ID: 20260923_0001
Revises: 20260922_0001
Create Date: 2026-09-23

Each row records what the pipeline predicted vs what the owner says is
right, linked to the source message. Corpus promotion stays MANUAL curation
(quality bar): this table is the intake queue, not an auto-training set.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260923_0001"
down_revision: str | None = "20260922_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE classification_feedback (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id uuid NOT NULL REFERENCES users(id),
        message_id uuid NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
        predicted_domain text NOT NULL DEFAULT '',
        predicted_importance text NOT NULL DEFAULT '',
        correct_domain text NOT NULL,
        correct_importance text NOT NULL DEFAULT '',
        note text NOT NULL DEFAULT '',
        created_at timestamptz NOT NULL DEFAULT now()
    )"""
    )
    op.execute(
        "CREATE INDEX idx_classification_feedback_message "
        "ON classification_feedback (message_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE classification_feedback")
