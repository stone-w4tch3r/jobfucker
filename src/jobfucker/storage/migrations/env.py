"""Alembic environment for the jobfucker storage schema.

Generates migrations **from the declarative models** in
``jobfucker.storage.models`` (``Base.metadata`` is authoritative); the schema the
app creates via ``create_schema`` and the migrations are the same four tables.

The target URL is ``JOBFUCKER_DB_URL`` when set (preferred for tests/dev),
falling back to the ``sqlalchemy.url`` main option in ``alembic.ini``.

NOTE: this directory is Alembic-generated tooling and is excluded from
basedpyright/ruff (see pyproject excludes). Do not hand-edit generated
``versions/*`` revisions.
"""

from __future__ import annotations

import logging
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from jobfucker.storage import models  # noqa: F401  (registers Base.metadata tables)

config = context.config

# Populate the SQLAlchemy portion of the config section from the models metadata.
if config.config_file_name is not None:
    # fileConfig() reprograms the whole logging tree from alembic.ini: it
    # replaces the root handlers with the ini's console handler, resets the root
    # level to `[logger_root]` (WARNING), and (by default) disables every
    # pre-existing logger. That is correct for a bare `uv run alembic` process,
    # but destructive when migrations run inside the app (CLI/GUI already
    # configured file/console logging BEFORE storage opens; a mid-command
    # fileConfig would silently drop the app's root level + handlers and the AI
    # DEBUG logs along with them). So when the app has configured logging, the
    # root level + handlers are restored and pre-existing loggers stay enabled;
    # only the alembic logger itself is pinned to WARNING (the ini would set it
    # to INFO and spew migration noise on every fresh-DB open).
    root_logger = logging.getLogger()
    app_logging_configured = root_logger.level != logging.NOTSET or bool(root_logger.handlers)
    root_level = root_logger.level
    root_handlers = root_logger.handlers.copy()
    fileConfig(config.config_file_name, disable_existing_loggers=False)
    if app_logging_configured:
        root_logger.setLevel(root_level)
        root_logger.handlers[:] = root_handlers
    logging.getLogger("alembic").setLevel(logging.WARNING)

target_metadata = models.Base.metadata


def _database_url() -> str:
    """Return the target URL: env override first, else the ini main option."""
    env_url = os.environ.get("JOBFUCKER_DB_URL")
    if env_url:
        return env_url
    url = config.get_main_option("sqlalchemy.url")
    if url is None:
        raise RuntimeError("sqlalchemy.url is not configured")
    return url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a live DB)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a real SQLite DB."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
