"""initial schema — docs/05_Backend_Schema.md (F-002)

Revision ID: 20260905_0001
Revises:
Create Date: 2026-09-05

Implements the authoritative schema: enums (§2), all tables (§3), indexes (§4/§7).
`created_at/updated_at` shorthand from the doc is expanded to two columns each.
State transitions are enforced in the state-transition layer, not by triggers.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260905_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = "timestamptz NOT NULL DEFAULT now()"

ENUMS: list[str] = [
    "CREATE TYPE group_category AS ENUM ('PLACEMENT','ACADEMIC','ADMINISTRATIVE','GENERAL','OTHER')",
    "CREATE TYPE instance_status AS ENUM ('PENDING','QR_PENDING','CONNECTED','RECONNECTING','DISCONNECTED','LOGGED_OUT','ERROR')",
    "CREATE TYPE message_state AS ENUM ('RECEIVED','VALIDATED','QUEUED','PROCESSING','PROCESSED','FAILED','RETRYING')",
    "CREATE TYPE attachment_state AS ENUM ('PENDING','DOWNLOADING','DOWNLOADED','PROCESSING','PROCESSED','FAILED','REJECTED_OVERSIZE','NEEDS_REVIEW')",
    "CREATE TYPE msg_domain AS ENUM ('PLACEMENT','ACADEMIC','EXAMINATION','ADMINISTRATIVE','EVENT','GENERAL','UNKNOWN')",
    "CREATE TYPE importance AS ENUM ('CRITICAL','HIGH','MEDIUM','LOW','IGNORE')",
    "CREATE TYPE eligibility_state AS ENUM ('UNKNOWN','MATCHED','NOT_FOUND','AMBIGUOUS','USER_CONFIRMED','ELIGIBLE','NOT_ELIGIBLE','EXPIRED','SUPERSEDED')",
    "CREATE TYPE match_status AS ENUM ('MATCHED','NOT_FOUND','AMBIGUOUS','INVALID_SOURCE')",
    "CREATE TYPE match_method AS ENUM ('EXACT','NORMALIZED','IDENTIFIER','FUZZY','MODEL_REVIEW')",
    "CREATE TYPE company_watch AS ENUM ('NONE','WATCHING','MUTED')",
    "CREATE TYPE event_type AS ENUM ('REGISTRATION','FORM','KYC','OA','EXAM','INTERVIEW','SHORTLIST','RESULT','VENUE','DOCUMENT_SUBMISSION','JOINING','OTHER')",
    "CREATE TYPE event_status AS ENUM ('DETECTED','ACTIVE','COMPLETED','CANCELLED','EXPIRED')",
    "CREATE TYPE deadline_state AS ENUM ('OPEN','DUE_SOON','EXPIRED','CANCELLED','COMPLETED')",
    "CREATE TYPE notification_priority AS ENUM ('CRITICAL','HIGH','MEDIUM','LOW','IGNORE')",
    "CREATE TYPE notification_status AS ENUM ('PENDING','QUEUED','SENT','FAILED','PENDING_DELIVERY','SUPPRESSED')",
    "CREATE TYPE memory_scope AS ENUM ('COMPANY','EVENT','PROFILE','GROUP','GENERAL')",
    "CREATE TYPE action_status AS ENUM ('PROPOSED','WAITING_APPROVAL','APPROVED','EXECUTING','SUCCEEDED','REJECTED','EXPIRED','FAILED')",
    "CREATE TYPE job_state AS ENUM ('QUEUED','STARTED','RETRYING','SUCCEEDED','FAILED','DEAD_LETTERED')",
]

TABLES: list[str] = [
    # --- users & profile (FR-PRO-001..003, SEC-002) ---
    f"""CREATE TABLE users (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        email text UNIQUE,
        display_name text NOT NULL,
        timezone text NOT NULL DEFAULT 'Asia/Kolkata',
        notification_preferences jsonb NOT NULL DEFAULT '{{}}',
        deleted_at timestamptz,
        created_at {TS},
        updated_at {TS}
    )""",
    f"""CREATE TABLE candidate_profiles (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id uuid NOT NULL UNIQUE REFERENCES users(id),
        canonical_name text NOT NULL,
        roll_number text,
        registration_number text,
        student_id text,
        branch text,
        batch text,
        cgpa numeric(4,2),
        tenth_percent numeric(5,2),
        twelfth_percent numeric(5,2),
        backlog_count smallint DEFAULT 0,
        other_attributes jsonb NOT NULL DEFAULT '{{}}',
        created_at {TS},
        updated_at {TS}
    )""",
    f"""CREATE TABLE identity_aliases (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        profile_id uuid NOT NULL REFERENCES candidate_profiles(id) ON DELETE CASCADE,
        alias text NOT NULL,
        kind text NOT NULL DEFAULT 'name_variant',
        created_at {TS},
        UNIQUE (profile_id, alias)
    )""",
    "CREATE INDEX idx_identity_aliases_alias ON identity_aliases (alias)",
    f"""CREATE TABLE profile_documents (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        profile_id uuid NOT NULL REFERENCES candidate_profiles(id),
        kind text NOT NULL,
        storage_key text NOT NULL,
        access_level text NOT NULL DEFAULT 'private',
        deleted_at timestamptz,
        created_at {TS},
        updated_at {TS}
    )""",
    # --- connector & groups (FR-WA-001..005, SEC-006, DEC-003) ---
    f"""CREATE TABLE whatsapp_instances (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        instance_key text NOT NULL UNIQUE,
        provider text NOT NULL DEFAULT 'evolution',
        status instance_status NOT NULL DEFAULT 'PENDING',
        secret_ref text NOT NULL,
        last_seen_at timestamptz,
        last_error text,
        created_at {TS},
        updated_at {TS}
    )""",
    f"""CREATE TABLE groups (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        provider_group_id text NOT NULL UNIQUE,
        name text NOT NULL,
        category group_category NOT NULL DEFAULT 'OTHER',
        enabled boolean NOT NULL DEFAULT false,
        priority smallint NOT NULL DEFAULT 5,
        participant_meta jsonb,
        deleted_at timestamptz,
        created_at {TS},
        updated_at {TS}
    )""",
    "CREATE INDEX idx_groups_enabled ON groups (enabled) WHERE enabled",
    # --- messages (FR-MSG-001..004, FR-DED-001, DEC-009, SEC-007, NFR-002) ---
    f"""CREATE TABLE messages (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        provider_message_id text NOT NULL,
        group_id uuid NOT NULL REFERENCES groups(id),
        sender_id text NOT NULL,
        sender_name text,
        sent_at timestamptz NOT NULL,
        text text,
        content_hash text NOT NULL,
        image_phash text,
        domain msg_domain,
        importance importance,
        classification jsonb,
        processing_state message_state NOT NULL DEFAULT 'RECEIVED',
        failure_reason text,
        correlation_id uuid NOT NULL,
        retention_expires_at timestamptz,
        received_at timestamptz NOT NULL DEFAULT now(),
        created_at {TS},
        updated_at {TS},
        UNIQUE (provider_message_id, group_id)
    )""",
    "CREATE INDEX idx_messages_group_sent ON messages (group_id, sent_at DESC)",
    """CREATE INDEX idx_messages_state ON messages (processing_state)
       WHERE processing_state NOT IN ('PROCESSED','FAILED')""",
    "CREATE INDEX idx_messages_correlation ON messages (correlation_id)",
    "CREATE INDEX idx_messages_content_hash ON messages (content_hash, sent_at DESC)",
    f"""CREATE TABLE attachments (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        message_id uuid NOT NULL REFERENCES messages(id),
        mime_type text NOT NULL,
        file_name text,
        storage_key text,
        checksum text,
        size_bytes bigint,
        processing_state attachment_state NOT NULL DEFAULT 'PENDING',
        failure_reason text,
        retention_expires_at timestamptz,
        created_at {TS},
        updated_at {TS}
    )""",
    """CREATE INDEX idx_attachments_state ON attachments (processing_state)
       WHERE processing_state IN ('PENDING','DOWNLOADING','PROCESSING','FAILED')""",
    f"""CREATE TABLE raw_event_payloads (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        message_id uuid REFERENCES messages(id) ON DELETE SET NULL,
        source text NOT NULL DEFAULT 'evolution',
        payload jsonb NOT NULL,
        retention_expires_at timestamptz NOT NULL,
        created_at {TS}
    )""",
    f"""CREATE TABLE idempotency_keys (
        key text PRIMARY KEY,
        fingerprint text NOT NULL,
        entity_ref text,
        created_at {TS}
    )""",
    # --- document intelligence (master §8, FR-ELG-002..004) ---
    f"""CREATE TABLE document_extractions (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        attachment_id uuid NOT NULL REFERENCES attachments(id),
        extractor text NOT NULL,
        extractor_version text NOT NULL,
        raw_text text,
        structured_payload jsonb,
        confidence numeric(4,3),
        evidence jsonb,
        needs_review boolean NOT NULL DEFAULT false,
        created_at {TS}
    )""",
    "CREATE INDEX idx_document_extractions_attachment ON document_extractions (attachment_id)",
    # --- companies & eligibility (FR-ELG-005..009, FR-MEM-001..005, ADR-005) ---
    f"""CREATE TABLE companies (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        canonical_name text NOT NULL,
        normalized_key text NOT NULL UNIQUE,
        watch_state company_watch NOT NULL DEFAULT 'NONE',
        lifecycle_stage text,
        first_seen_at timestamptz,
        created_at {TS},
        updated_at {TS}
    )""",
    f"""CREATE TABLE company_aliases (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
        alias text NOT NULL,
        UNIQUE (company_id, alias)
    )""",
    "CREATE INDEX idx_company_aliases_alias ON company_aliases (alias)",
    f"""CREATE TABLE eligibility_records (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id uuid NOT NULL REFERENCES users(id),
        company_id uuid NOT NULL REFERENCES companies(id),
        state eligibility_state NOT NULL DEFAULT 'UNKNOWN',
        match_status match_status,
        match_method match_method,
        confidence numeric(4,3),
        requires_user_review boolean NOT NULL DEFAULT false,
        source_message_id uuid REFERENCES messages(id),
        source_document_id uuid REFERENCES document_extractions(id),
        detected_at timestamptz NOT NULL DEFAULT now(),
        superseded_by_id uuid REFERENCES eligibility_records(id),
        correction_note text,
        created_at {TS},
        updated_at {TS}
    )""",
    "CREATE INDEX idx_eligibility_user_company ON eligibility_records (user_id, company_id, detected_at DESC)",
    """CREATE INDEX idx_eligibility_review ON eligibility_records (requires_user_review)
       WHERE requires_user_review""",
    # --- events, updates, deadlines (FR-EVT-001..005, FR-DED-003/004, ADR-006) ---
    f"""CREATE TABLE events (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        company_id uuid REFERENCES companies(id),
        group_id uuid REFERENCES groups(id),
        type event_type NOT NULL,
        status event_status NOT NULL DEFAULT 'DETECTED',
        canonical_key text NOT NULL UNIQUE,
        title text,
        current_payload jsonb NOT NULL DEFAULT '{{}}',
        start_at timestamptz,
        deadline_at timestamptz,
        timezone text NOT NULL DEFAULT 'Asia/Kolkata',
        created_at {TS},
        updated_at {TS}
    )""",
    "CREATE INDEX idx_events_deadline ON events (deadline_at) WHERE status = 'ACTIVE'",
    "CREATE INDEX idx_events_company ON events (company_id, created_at DESC)",
    f"""CREATE TABLE event_updates (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        event_id uuid NOT NULL REFERENCES events(id),
        source_message_id uuid REFERENCES messages(id),
        delta jsonb NOT NULL,
        detected_at timestamptz NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX idx_event_updates_event ON event_updates (event_id, detected_at)",
    f"""CREATE TABLE deadlines (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        event_id uuid NOT NULL UNIQUE REFERENCES events(id) ON DELETE CASCADE,
        due_at timestamptz NOT NULL,
        state deadline_state NOT NULL DEFAULT 'OPEN',
        reminder_policy jsonb NOT NULL DEFAULT '{{"windows_hours": [24, 6, 1]}}',
        reminders_sent jsonb NOT NULL DEFAULT '[]',
        created_at {TS},
        updated_at {TS}
    )""",
    "CREATE INDEX idx_deadlines_state_due ON deadlines (state, due_at)",
    # --- notifications (FR-NOT-001..007, DEC-002, NFR-005) ---
    f"""CREATE TABLE notifications (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id uuid NOT NULL REFERENCES users(id),
        event_id uuid REFERENCES events(id),
        message_id uuid REFERENCES messages(id),
        eligibility_id uuid REFERENCES eligibility_records(id),
        priority notification_priority NOT NULL,
        status notification_status NOT NULL DEFAULT 'PENDING',
        reason text NOT NULL,
        dedup_key text NOT NULL,
        payload jsonb NOT NULL,
        evidence_refs jsonb NOT NULL DEFAULT '[]',
        scheduled_for timestamptz,
        sent_at timestamptz,
        created_at {TS},
        updated_at {TS},
        UNIQUE (dedup_key)
    )""",
    "CREATE INDEX idx_notifications_user_time ON notifications (user_id, created_at DESC)",
    """CREATE INDEX idx_notifications_pending ON notifications (status, scheduled_for)
       WHERE status IN ('PENDING','QUEUED','PENDING_DELIVERY')""",
    f"""CREATE TABLE notification_deliveries (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        notification_id uuid NOT NULL REFERENCES notifications(id),
        channel text NOT NULL,
        status text NOT NULL,
        error text,
        attempted_at timestamptz NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX idx_deliveries_notification ON notification_deliveries (notification_id)",
    # --- memory, AI logs, actions (future), jobs, audit (master §9/§12/§14, SEC-004/005) ---
    f"""CREATE TABLE memory_records (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        scope memory_scope NOT NULL,
        subject text NOT NULL,
        predicate text NOT NULL,
        object_value jsonb NOT NULL,
        source_message_id uuid REFERENCES messages(id),
        confidence numeric(4,3),
        valid_from timestamptz,
        valid_until timestamptz,
        created_at {TS}
    )""",
    "CREATE INDEX idx_memory_subject ON memory_records (scope, subject)",
    f"""CREATE TABLE ai_call_logs (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        correlation_id uuid NOT NULL,
        task text NOT NULL,
        provider text NOT NULL,
        model text NOT NULL,
        latency_ms integer NOT NULL,
        prompt_tokens integer,
        completion_tokens integer,
        validation text NOT NULL,
        retention_expires_at timestamptz,
        created_at {TS}
    )""",
    "CREATE INDEX idx_ai_calls_task_time ON ai_call_logs (task, created_at DESC)",
    f"""CREATE TABLE actions (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id uuid NOT NULL REFERENCES users(id),
        type text NOT NULL,
        target text,
        payload jsonb NOT NULL DEFAULT '{{}}',
        risk_level text NOT NULL DEFAULT 'low',
        status action_status NOT NULL DEFAULT 'PROPOSED',
        approval_required boolean NOT NULL DEFAULT true,
        created_at {TS},
        updated_at {TS}
    )""",
    f"""CREATE TABLE job_records (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        queue text NOT NULL,
        job_type text NOT NULL,
        state job_state NOT NULL DEFAULT 'QUEUED',
        attempts smallint NOT NULL DEFAULT 0,
        max_attempts smallint NOT NULL DEFAULT 5,
        error_class text,
        last_error text,
        correlation_id uuid,
        started_at timestamptz,
        finished_at timestamptz,
        created_at {TS}
    )""",
    "CREATE INDEX idx_jobs_state ON job_records (queue, state)",
    f"""CREATE TABLE dead_letter_queue (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        job_record_id uuid REFERENCES job_records(id),
        queue text NOT NULL,
        job_type text NOT NULL,
        payload jsonb NOT NULL,
        error_class text NOT NULL,
        last_error text,
        correlation_id uuid,
        moved_at timestamptz NOT NULL DEFAULT now(),
        resolved_at timestamptz
    )""",
    f"""CREATE TABLE audit_logs (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        actor text NOT NULL,
        action text NOT NULL,
        entity_type text NOT NULL,
        entity_id uuid,
        result text NOT NULL,
        metadata jsonb NOT NULL DEFAULT '{{}}',
        correlation_id uuid,
        created_at {TS}
    )""",
    "CREATE INDEX idx_audit_entity ON audit_logs (entity_type, entity_id, created_at DESC)",
    "CREATE INDEX idx_audit_actor ON audit_logs (actor, created_at DESC)",
]

# pgvector is OPTIONAL (ADR-003): created only where the extension is available,
# and never authoritative for eligibility/deadlines.
PGVECTOR_BLOCK = """
DO $pia$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector') THEN
        CREATE EXTENSION IF NOT EXISTS vector;
        CREATE TABLE IF NOT EXISTS message_embeddings (
            message_id uuid PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
            embedding vector(1024) NOT NULL,
            model text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        );
    END IF;
END
$pia$;
"""

DROP_ORDER: list[str] = [
    "message_embeddings",
    "audit_logs",
    "dead_letter_queue",
    "job_records",
    "actions",
    "ai_call_logs",
    "memory_records",
    "notification_deliveries",
    "notifications",
    "deadlines",
    "event_updates",
    "events",
    "eligibility_records",
    "company_aliases",
    "companies",
    "document_extractions",
    "idempotency_keys",
    "raw_event_payloads",
    "attachments",
    "messages",
    "groups",
    "whatsapp_instances",
    "profile_documents",
    "identity_aliases",
    "candidate_profiles",
    "users",
]

ENUM_TYPES: list[str] = [
    "group_category", "instance_status", "message_state", "attachment_state",
    "msg_domain", "importance", "eligibility_state", "match_status", "match_method",
    "company_watch", "event_type", "event_status", "deadline_state",
    "notification_priority", "notification_status", "memory_scope", "action_status",
    "job_state",
]


def upgrade() -> None:
    for statement in ENUMS:
        op.execute(statement)
    for statement in TABLES:
        op.execute(statement)
    op.execute(PGVECTOR_BLOCK)


def downgrade() -> None:
    for table in DROP_ORDER:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for enum_type in ENUM_TYPES:
        op.execute(f"DROP TYPE IF EXISTS {enum_type}")
