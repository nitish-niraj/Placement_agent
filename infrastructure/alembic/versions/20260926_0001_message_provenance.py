"""Message provenance: reply edges + caption/body source (audit-only).

Revision ID: 20260926_0001
Revises: 20260923_0001
Create Date: 2026-09-26

- reply_to_provider_message_id / reply_to_sender_id / reply_quoted_text:
  WhatsApp reply edge (contextInfo.stanzaId), persisted at ingestion.
- reply_to_message_id: resolved parent row (same-group scope), backfilled
  opportunistically (late parents link on their own insert).
- text_source: which body shape won (conversation|extended_text|
  image_caption|video_caption|document_caption), NULL for legacy/media-only.
- has_media: attachment present at ingestion (avoids a JOIN per read).

All nullable/additive — no breaking change. Evaluation keeps reading
messages.text unchanged; reply_quoted_text is audit-only, never classified.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260926_0001"
down_revision: str | None = "20260923_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE messages ADD COLUMN text_source text"
    )
    op.execute(
        "ALTER TABLE messages ADD COLUMN has_media boolean NOT NULL DEFAULT FALSE"
    )
    op.execute(
        "ALTER TABLE messages ADD COLUMN reply_to_provider_message_id text"
    )
    op.execute(
        "ALTER TABLE messages ADD COLUMN reply_to_sender_id text"
    )
    op.execute(
        "ALTER TABLE messages ADD COLUMN reply_to_message_id uuid "
        "REFERENCES messages(id) ON DELETE SET NULL"
    )
    op.execute(
        "ALTER TABLE messages ADD COLUMN reply_quoted_text text"
    )
    op.execute(
        "CREATE INDEX idx_messages_text_source ON messages (text_source) "
        "WHERE text_source IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX idx_messages_reply_parent ON messages (reply_to_message_id) "
        "WHERE reply_to_message_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX idx_messages_reply_stanza ON messages "
        "(group_id, reply_to_provider_message_id) "
        "WHERE reply_to_provider_message_id IS NOT NULL"
    )
    # Backfill: media flag from existing attachments; reply parents from
    # stanza ids where the parent already exists in the same group.
    op.execute(
        "UPDATE messages m SET has_media = TRUE WHERE EXISTS "
        "(SELECT 1 FROM attachments a WHERE a.message_id = m.id)"
    )
    op.execute(
        "UPDATE messages m SET reply_to_message_id = p.id FROM messages p "
        "WHERE p.provider_message_id = m.reply_to_provider_message_id "
        "AND p.group_id = m.group_id AND m.reply_to_message_id IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX idx_messages_reply_stanza")
    op.execute("DROP INDEX idx_messages_reply_parent")
    op.execute("DROP INDEX idx_messages_text_source")
    op.execute("ALTER TABLE messages DROP COLUMN reply_quoted_text")
    op.execute("ALTER TABLE messages DROP COLUMN reply_to_message_id")
    op.execute("ALTER TABLE messages DROP COLUMN reply_to_sender_id")
    op.execute("ALTER TABLE messages DROP COLUMN reply_to_provider_message_id")
    op.execute("ALTER TABLE messages DROP COLUMN has_media")
    op.execute("ALTER TABLE messages DROP COLUMN text_source")
