"""Isolated test database for integration tests.

Each test gets a scratch `pia_test_*` database on the same Postgres server
(migrations applied, one seed user), so integration runs never touch live
placement data and never depend on per-test DELETE cleanup. Requires the
compose stack (same gate as the suites themselves).
"""

import os
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest
import sqlalchemy
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig

REPO_ROOT = Path(__file__).resolve().parents[2]


def _server_url() -> str:
    """Worker-resolved DB URL (explicit DATABASE_URL wins, else POSTGRES_*)."""
    from pia_worker import settings as worker_settings

    worker_settings.get_settings.cache_clear()
    try:
        return worker_settings.get_settings().database_url
    finally:
        worker_settings.get_settings.cache_clear()


def _with_dbname(url: str, name: str) -> str:
    parts = urlparse(url)
    return urlunparse(parts._replace(path="/" + name))


@pytest.fixture(scope="function")
def isolated_db(monkeypatch: pytest.MonkeyPatch) -> str:
    """Create, migrate, seed, yield, and drop a scratch database."""
    import pia_api.settings as api_settings
    import pia_worker.settings as worker_settings

    server_url = _server_url()
    parsed = urlparse(server_url)
    if not parsed.hostname:
        pytest.skip("no Postgres server configured for integration tests")
    db_name = f"pia_test_{os.getpid()}_{int(time.time() * 1000) % 1000000}"
    admin = sqlalchemy.create_engine(
        _with_dbname(server_url, "postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sqlalchemy.text(f'CREATE DATABASE "{db_name}"'))
    admin.dispose()

    test_url = _with_dbname(server_url, db_name)
    monkeypatch.setenv("DATABASE_URL", test_url)
    worker_settings.get_settings.cache_clear()
    monkeypatch.setattr(api_settings, "_settings", None)

    cfg = AlembicConfig(str(REPO_ROOT / "infrastructure" / "alembic"
                            / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", test_url)
    alembic_command.upgrade(cfg, "head")

    engine = sqlalchemy.create_engine(test_url)
    with engine.begin() as conn:
        conn.execute(sqlalchemy.text(
            "INSERT INTO users (email, display_name) VALUES "
            "('integration-test@example.com', 'Integration Test')"))
    yield test_url

    engine.dispose()
    worker_settings.get_settings.cache_clear()
    monkeypatch.setattr(api_settings, "_settings", None)
    admin = sqlalchemy.create_engine(
        _with_dbname(server_url, "postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sqlalchemy.text(
            f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
    admin.dispose()
