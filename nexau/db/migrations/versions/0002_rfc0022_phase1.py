# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""RFC-0022 Phase 1 — add idempotency_key + extra to agent_run_actions

Adds two nullable columns to ``agent_run_actions`` that nexau Phase 1
introduces for typed *Extra payloads (RUN_START / RUN_END / typed
ReplaceExtra variants) and streaming idempotency:

- ``idempotency_key VARCHAR(255)`` — UNIQUE-when-non-NULL. Streaming
  writers use ``"{run_id}:start"`` / ``"{run_id}:end"`` to make the
  RUN_START/END markers idempotent across retries.
- ``extra JSONB`` — typed *Extra payloads dispatched by ``action_type``
  (RunStartExtra / RunEndExtra / ReplaceExtra discriminated union).

## Idempotency note

For fresh databases ``SQLModel.metadata.create_all`` already created
``agent_run_actions`` with these columns (the model in master has them).
This migration uses ``ADD COLUMN IF NOT EXISTS`` semantics via batch
mode + a column-presence check so re-running on a fresh DB is a no-op,
matching ``upgrade_to_head``'s baseline-detect-then-stamp behaviour.

## SQLite ALTER caveat

SQLite ``ALTER TABLE ADD COLUMN`` always works for nullable columns.
But ALTER COLUMN / DROP COLUMN need batch mode. ``env.py`` enables
``render_as_batch=True`` so the migration is portable to PG and SQLite.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-06 (RFC-0022)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def _has_column(table_name: str, column_name: str) -> bool:
    """Return True if ``table_name.column_name`` already exists.

    Defensive guard: a fresh DB may have already created the columns via
    ``SQLModel.metadata.create_all`` before alembic is invoked. This
    keeps the migration idempotent without relying on dialect-specific
    ``IF NOT EXISTS`` syntax (SQLite doesn't have it for ADD COLUMN).
    """
    bind = op.get_bind()
    insp = inspect(bind)
    if table_name not in insp.get_table_names():
        return False
    return any(c["name"] == column_name for c in insp.get_columns(table_name))


def upgrade() -> None:
    # 1. idempotency_key — VARCHAR(255), nullable, UNIQUE when not NULL.
    if not _has_column("agent_run_actions", "idempotency_key"):
        op.add_column(
            "agent_run_actions",
            sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        )

    # 2. Partial unique index. Note that SQLite ignores the WHERE clause
    # but enforces uniqueness over the indexed columns; with NULLs treated
    # as distinct (SQLite default), multiple NULL idempotency_keys still
    # work correctly. PostgreSQL respects the WHERE clause as a partial
    # index, identical semantics.
    bind = op.get_bind()
    insp = inspect(bind)
    existing_indexes = {ix["name"] for ix in insp.get_indexes("agent_run_actions")}
    if "ix_agent_run_actions_idempotency_key_unique" not in existing_indexes:
        op.create_index(
            "ix_agent_run_actions_idempotency_key_unique",
            "agent_run_actions",
            ["idempotency_key"],
            unique=True,
            postgresql_where=sa.text("idempotency_key IS NOT NULL"),
            sqlite_where=sa.text("idempotency_key IS NOT NULL"),
        )

    # 3. extra — JSONB on PG, JSON on SQLite (sa.JSON dispatches per dialect).
    if not _has_column("agent_run_actions", "extra"):
        op.add_column(
            "agent_run_actions",
            sa.Column("extra", sa.JSON, nullable=True),
        )


def downgrade() -> None:
    # Drop in reverse order: index → columns. Idempotent guards mirror upgrade.
    bind = op.get_bind()
    insp = inspect(bind)
    existing_indexes = {ix["name"] for ix in insp.get_indexes("agent_run_actions")}
    if "ix_agent_run_actions_idempotency_key_unique" in existing_indexes:
        op.drop_index(
            "ix_agent_run_actions_idempotency_key_unique",
            table_name="agent_run_actions",
        )
    if _has_column("agent_run_actions", "extra"):
        op.drop_column("agent_run_actions", "extra")
    if _has_column("agent_run_actions", "idempotency_key"):
        op.drop_column("agent_run_actions", "idempotency_key")
