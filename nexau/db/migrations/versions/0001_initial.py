# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""initial — pre-Phase-1 baseline marker

This is an empty migration that represents the pre-RFC-0022-Phase-1
schema baseline.

History: nexau's session-DB schema was historically bootstrapped via
``SQLModel.metadata.create_all`` (a CREATE TABLE IF NOT EXISTS path that
isn't suited for evolving schemas). RFC-0022 introduced the first schema
change that didn't fit cleanly into that model — adding two columns
(``idempotency_key`` + ``extra``) to ``agent_run_actions`` — and so
nexau adopted alembic for schema evolution from this point forward.

This revision is intentionally a no-op:

- Fresh DB: ``SQLModel.metadata.create_all`` (called from
  ``SQLDatabaseEngine.setup_models``) bootstraps all tables, then
  ``upgrade_to_head`` detects the schema is already at the target state
  and stamps the right baseline.
- Legacy pre-Phase-1 DB: ``upgrade_to_head`` detects via
  ``_detect_baseline_revision`` that ``agent_run_actions`` exists without
  ``idempotency_key`` and stamps this revision (0001) as the starting
  point, then runs 0002 to add the columns.

Revision ID: 0001
Revises:
Create Date: 2026-05-06 (RFC-0022)
"""

from __future__ import annotations

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """No-op: pre-Phase-1 baseline.

    See module docstring for rationale.
    """


def downgrade() -> None:
    """No-op: alembic baseline cannot be downgraded further."""
