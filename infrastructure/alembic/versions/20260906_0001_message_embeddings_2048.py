"""message_embeddings 1024 -> 2048 dims — P12 (F-028)

Revision ID: 20260906_0001
Revises: 20260905_0002
Create Date: 2026-09-06

The original vector(1024) assumed nvidia/nv-embedqa-e5-v5, since retired from
the NIM catalog (410 Gone). The live model is nvidia/nemotron-3-embed-1b
(2048 dims, DEC-006 model churn). Table is empty at migration time, so the
type change is trivially safe; stored vectors would require re-embedding.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260906_0001"
down_revision: str | None = "20260905_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("TRUNCATE message_embeddings")
    op.execute("ALTER TABLE message_embeddings ALTER COLUMN embedding TYPE vector(2048)")


def downgrade() -> None:
    op.execute("TRUNCATE message_embeddings")
    op.execute("ALTER TABLE message_embeddings ALTER COLUMN embedding TYPE vector(1024)")
