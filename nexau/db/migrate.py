# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Programmatic alembic driver for nexau session DB schema (RFC-0022).

nexau is a *library*, not a service — users `pip install nexau` and call
``Agent(...).run(...)``; they don't run separate CLI migration commands.
This module wraps alembic so schema migrations apply transparently inside
``setup_models``.

Key responsibilities:
- Locate the bundled ``migrations/`` directory (works for editable installs
  AND wheels via ``importlib.resources.files``).
- Provide a programmatic ``upgrade_to_head`` that synchronously applies all
  pending migrations against a given ``DATABASE_URL`` (sync URL — alembic
  runs migrations on a sync connection even when the application uses
  async).
- Detect a *legacy* database (existing tables but no ``alembic_version``
  table) and stamp the right baseline revision so subsequent upgrades
  behave the same as on a fresh DB.

Environment overrides:
- ``NEXAU_AUTO_MIGRATE=off`` — caller must run migrations explicitly,
  ``setup_models`` only checks the DB is at head and fails fast otherwise.
- ``NEXAU_AUTO_MIGRATE=auto`` (default) — apply pending migrations on
  ``setup_models``.
"""

from __future__ import annotations

import logging
import os
from importlib.resources import files
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, inspect
from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)


# ============================================================================
# Bundled migrations directory location
# ============================================================================


def _migrations_dir() -> Path:
    """Return the path to the packaged migrations directory.

    Works for editable installs (uv pip install -e .) and built wheels
    (pip install nexau). ``importlib.resources.files`` resolves the
    package data location regardless of install mode.
    """
    return Path(str(files("nexau.db") / "migrations"))


def _build_config(database_url: str) -> Config:
    """Build an alembic Config object pointing at the packaged migrations.

    We don't use ``alembic.ini`` on disk — alembic supports configuring
    via API. Saves users the discovery cost of "where did this config
    file come from" when debugging.
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(_migrations_dir()))
    cfg.set_main_option("sqlalchemy.url", database_url)
    # Match alembic 1.x default. nexau migration files use only sync ops.
    cfg.set_main_option("file_template", "%%(rev)s_%%(slug)s")
    return cfg


# ============================================================================
# Sync vs async URL bridging
# ============================================================================


def _to_sync_url(database_url: str) -> str:
    """Convert an async SQLAlchemy URL to its sync equivalent.

    nexau's ``SQLDatabaseEngine`` uses ``sqlite+aiosqlite`` /
    ``postgresql+asyncpg``. alembic's migration runner needs a sync URL.
    The mapping is simple — just drop the async driver suffix.
    """
    replacements = {
        "sqlite+aiosqlite://": "sqlite://",
        "postgresql+asyncpg://": "postgresql+psycopg2://",
        "postgresql+asyncpg+psycopg://": "postgresql+psycopg://",
        "mysql+aiomysql://": "mysql+pymysql://",
    }
    for async_prefix, sync_prefix in replacements.items():
        if database_url.startswith(async_prefix):
            return sync_prefix + database_url[len(async_prefix) :]
    return database_url


# ============================================================================
# Baseline detection: stamp pre-migration databases at the right revision
# ============================================================================


# Maps a recognizable schema fingerprint to the revision id that schema
# corresponds to. Order matters: more-specific fingerprints first.
#
# These ids are the long-form alembic revision hashes from
# ``migrations/versions/*.py``. Keep this list aligned with new revisions
# whenever a migration adds a fingerprint-detectable column.
_BASELINE_FINGERPRINTS: list[tuple[str, str]] = [
    # If table has the Phase 1 columns already, schema is at 0002.
    ("0002_rfc0022_phase1", "0002"),
]


def _detect_baseline_revision(conn: Connection) -> str | None:
    """Return the revision id that the current schema corresponds to.

    Returns None if the DB is empty (caller will run from scratch) or if
    the schema doesn't match any known baseline (caller should fail loud).
    """
    insp = inspect(conn)
    tables = set(insp.get_table_names())

    # 0. Empty DB → no baseline, run all migrations from scratch.
    if not tables:
        return None

    # If alembic_version already exists, nothing to detect — caller path
    # uses the recorded revision.
    if "alembic_version" in tables:
        return None  # caller skips stamp + lets alembic upgrade from there

    # 1. agent_run_actions exists with idempotency_key + extra → at 0002.
    if "agent_run_actions" in tables:
        cols = {c["name"] for c in insp.get_columns("agent_run_actions")}
        if {"idempotency_key", "extra"}.issubset(cols):
            return "0002"
        # 2. agent_run_actions exists without those cols → pre-Phase-1, at 0001.
        return "0001"

    # 3. Some other tables present but no agent_run_actions — caller path
    # may be a non-nexau schema or a fresh DB with other models registered;
    # safest is to start from scratch (run all migrations).
    return None


# ============================================================================
# Public API
# ============================================================================


def current_revision(database_url: str) -> str | None:
    """Return the alembic revision id of the database, or None if uninitialized."""
    sync_url = _to_sync_url(database_url)
    engine: Engine = create_engine(sync_url)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            return ctx.get_current_revision()
    finally:
        engine.dispose()


def upgrade_to_head(database_url: str) -> None:
    """Apply all pending migrations to ``database_url``.

    Pre-existing databases (created by ``SQLModel.metadata.create_all``
    before nexau bundled alembic) are stamped at the right baseline before
    upgrading, so the migration is idempotent and safe to re-run.

    Honored env vars:
      ``NEXAU_AUTO_MIGRATE=off``   — skip the upgrade; only verify DB is
                                     at head, fail loud otherwise.
      ``NEXAU_AUTO_MIGRATE=auto``  — (default) stamp baseline if needed,
                                     run upgrade head.
    """
    mode = os.environ.get("NEXAU_AUTO_MIGRATE", "auto").lower()
    if mode == "off":
        _verify_at_head_or_fail(database_url)
        return

    # ``:memory:`` SQLite DBs are scoped to a single connection — alembic
    # opens its own sync connection separate from the application's async
    # one, so it would see an empty DB and fail to ALTER non-existent
    # tables. ``setup_models``'s ``create_all`` already lays down the head
    # schema in such cases; skip alembic entirely for transient DBs.
    if ":memory:" in database_url:
        logger.debug("nexau db is :memory: — skipping alembic upgrade (transient DB)")
        return

    sync_url = _to_sync_url(database_url)
    cfg = _build_config(sync_url)
    script_dir = ScriptDirectory.from_config(cfg)
    head_rev = script_dir.get_current_head()

    engine: Engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            current = ctx.get_current_revision()

            # 1. Already at head — nothing to do.
            if current == head_rev:
                logger.debug("nexau db at head revision %s, no migration", head_rev)
                return

            # 2. No alembic_version row — either fresh DB or legacy nexau DB.
            #    Detect which by looking at table fingerprints.
            if current is None:
                baseline = _detect_baseline_revision(conn)
                if baseline is not None:
                    logger.info(
                        "nexau db detected at baseline revision %s — stamping then upgrading to %s",
                        baseline,
                        head_rev,
                    )
                    # Stamp the detected baseline as the starting point
                    # (alembic skips applying these scripts).
                    from alembic import command

                    command.stamp(cfg, baseline)
                else:
                    logger.info("nexau db is empty — running full migration to %s", head_rev)

        # Now safely upgrade. Use a fresh connection because alembic command
        # API takes the cfg and manages its own connection.
        from alembic import command

        command.upgrade(cfg, "head")
        logger.info("nexau db migrated to revision %s", head_rev)
    finally:
        engine.dispose()


def _verify_at_head_or_fail(database_url: str) -> None:
    sync_url = _to_sync_url(database_url)
    cfg = _build_config(sync_url)
    head_rev = ScriptDirectory.from_config(cfg).get_current_head()
    current = current_revision(database_url)
    if current != head_rev:
        raise RuntimeError(
            f"nexau db is at revision {current!r} but expected {head_rev!r}. "
            "NEXAU_AUTO_MIGRATE is set to 'off' — apply migrations manually with "
            "`python -m nexau.db.cli upgrade head` before starting the agent."
        )
