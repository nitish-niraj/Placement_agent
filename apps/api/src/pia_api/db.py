"""Shared SQLAlchemy engine (sync) for API request handlers."""

from functools import lru_cache

import sqlalchemy

from pia_api.settings import get_settings


@lru_cache(maxsize=4)
def _engine_cached(url: str) -> sqlalchemy.Engine:
    return sqlalchemy.create_engine(
        url, pool_pre_ping=True, connect_args={"connect_timeout": 3}
    )


def get_engine() -> sqlalchemy.Engine:
    return _engine_cached(get_settings().database_url)
