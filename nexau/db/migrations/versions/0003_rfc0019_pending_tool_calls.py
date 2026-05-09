# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""rfc0019 — sessions.pending_tool_calls JSON column

RFC-0019 (PR #481) added a ``pending_tool_calls`` JSON field to
``SessionModel`` so the framework can persist held tool-call decisions
across the user's allow/deny prompt. Fresh DBs pick this up via
``SQLModel.metadata.create_all``, but **legacy DBs whose ``sessions``
table pre-dates #481 silently lack the column** — the SQL hint file
``nexau/archs/session/migrations/001_tool_permission.sql`` only carries
a *comment* about a manual ``ALTER TABLE``; no Python code performs
the ALTER. First call to ``session.pending_tool_calls`` then crashes
with ``OperationalError: no such column``.

This migration closes that gap. Idempotent against fresh DBs (the
column already exists from create_all → no-op).

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-08 (RFC-0019 / RFC-0022 coexistence)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def _has_column(table_name: str, column_name: str) -> bool:
    """Return True if ``table_name.column_name`` already exists.

    Same defensive pattern as 0002: a fresh DB may have created the
    column via ``metadata.create_all`` before alembic runs, so we skip
    the ADD COLUMN to avoid dialect-specific "duplicate column" errors.
    """
    bind = op.get_bind()
    insp = inspect(bind)
    if table_name not in insp.get_table_names():
        return False
    return any(c["name"] == column_name for c in insp.get_columns(table_name))


def upgrade() -> None:
    # ``sessions`` may not exist if the deployer disabled session
    # persistence entirely (in-memory only) — guard against that too.
    bind = op.get_bind()
    insp = inspect(bind)
    if "sessions" not in insp.get_table_names():
        return

    if not _has_column("sessions", "pending_tool_calls"):
        # sa.JSON dispatches to JSONB on PG, JSON (TEXT-backed) on SQLite —
        # matches the SQLModel field declaration in
        # ``nexau/archs/session/models/session.py``.
        op.add_column(
            "sessions",
            sa.Column("pending_tool_calls", sa.JSON, nullable=True),
        )


def downgrade() -> None:
    if _has_column("sessions", "pending_tool_calls"):
        op.drop_column("sessions", "pending_tool_calls")
