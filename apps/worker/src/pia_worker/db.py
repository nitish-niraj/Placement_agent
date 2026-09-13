"""Shared worker DB engine helpers.

The worker runs in two contexts: in-container (DATABASE_URL points at the
compose network) and on the host (the Teams listener / form executor CLI —
no DATABASE_URL in .env, so the URL is assembled from POSTGRES_* with
localhost). One helper serves both."""

import os
from urllib.parse import quote

import sqlalchemy


def host_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    user = quote(os.environ.get("POSTGRES_USER", "pia"))
    password = quote(os.environ.get("POSTGRES_PASSWORD", "pia"))
    db = os.environ.get("POSTGRES_DB", "pia")
    return f"postgresql+psycopg://{user}:{password}@localhost:5432/{db}"


def engine_for_current_host() -> sqlalchemy.Engine:
    return sqlalchemy.create_engine(host_database_url())
