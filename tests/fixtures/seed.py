"""Deterministic fixture seed — F-002 acceptance ("seed command loads fixtures").

Usage (after `alembic upgrade head`):
    python -m tests.fixtures.seed

Idempotent: safe to run repeatedly (ON CONFLICT DO NOTHING). Data mirrors the docs'
running examples so fixtures and documentation stay aligned (roll 21CS1042 etc.).
"""

import os
import uuid

import sqlalchemy

USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
PROFILE_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
GROUPS = [
    (
        uuid.UUID("00000000-0000-0000-0000-000000000101"),
        "group-placement@g.us",
        "Placement Cell 2026",
        "PLACEMENT",
        True,
    ),
    (
        uuid.UUID("00000000-0000-0000-0000-000000000102"),
        "group-academic@g.us",
        "Academic Notices",
        "ACADEMIC",
        True,
    ),
    (
        uuid.UUID("00000000-0000-0000-0000-000000000103"),
        "group-memes@g.us",
        "Memes & Chatter",
        "GENERAL",
        False,
    ),
]
ALIASES = ["niraj", "niraj kumar", "niraj k", "nirajkumar", "niraj kumar cse"]


def main() -> None:
    url = os.getenv("DATABASE_URL", "postgresql+psycopg://pia:pia@localhost:5432/pia")
    engine = sqlalchemy.create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            sqlalchemy.text(
                "INSERT INTO users (id, display_name, timezone, notification_preferences) "
                "VALUES (:id, 'Niraj', 'Asia/Kolkata', :prefs) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": USER_ID, "prefs": '{"digest_hour": 20, "digest_minute": 30}'},
        )
        conn.execute(
            sqlalchemy.text(
                "INSERT INTO candidate_profiles (id, user_id, canonical_name, roll_number, "
                "registration_number, branch, batch, cgpa) "
                "VALUES (:id, :user_id, 'Niraj Kumar', '21CS1042', '2021CS1042', "
                "'CSE', '2022-2026', 8.24) ON CONFLICT (id) DO NOTHING"
            ),
            {"id": PROFILE_ID, "user_id": USER_ID},
        )
        for alias in ALIASES:
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO identity_aliases (profile_id, alias) "
                    "VALUES (:profile_id, :alias) "
                    "ON CONFLICT (profile_id, alias) DO NOTHING"
                ),
                {"profile_id": PROFILE_ID, "alias": alias},
            )
        for group_id, jid, name, category, enabled in GROUPS:
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO groups (id, provider_group_id, name, category, enabled) "
                    "VALUES (:id, :jid, :name, CAST(:category AS group_category), :enabled) "
                    "ON CONFLICT (provider_group_id) DO NOTHING"
                ),
                {
                    "id": group_id,
                    "jid": jid,
                    "name": name,
                    "category": category,
                    "enabled": enabled,
                },
            )
    engine.dispose()
    print(
        f"Seeded: user niraj ({USER_ID}), "
        f"profile with {len(ALIASES)} aliases, {len(GROUPS)} groups"
    )


if __name__ == "__main__":
    main()
