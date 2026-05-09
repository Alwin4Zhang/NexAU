# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Alembic migration upgrade-path tests (RFC-0022).

Coverage matrix — every real-world DB shape an upgrade may encounter.
Add a new test (not edit) when introducing a new migration step.

Starting shape → expected outcome:

- empty file
  → create_all + stamp head, schema at head
  (test_fresh_db_setup_models_lands_at_head)
- agent_run_actions w/o idempotency_key/extra
  → stamp 0001, run 0002, cols added, head
  (test_legacy_pre_phase1_db_gets_columns_added)
- already at head
  → second call no-op
  (test_upgrade_is_idempotent_when_at_head)
- not at head + NEXAU_AUTO_MIGRATE=off
  → RuntimeError
  (test_auto_migrate_off_verifies_only)
- agent_run_actions WITH cols, no alembic_version
  → stamp 0002, NO DDL applied (NAC pre-bundled-alembic case)
  (test_pre_existing_table_with_phase1_cols_stamps_head_no_ddl)
- agent_run_actions w/ idempotency_key only
  → upgrade adds extra, _has_column skips existing
  (test_partial_migration_only_idempotency_key_completes_cleanly)
- alembic_version has unknown revision
  → upgrade raises (no silent stamp)
  (test_foreign_alembic_version_revision_fails_loud)
- fresh DB + register PermissionRuleModel + AgentRunActionModel
  → both tables created by ORM, alembic at head, no DDL on either
  (test_setup_models_with_permission_rule_lands_at_head)
- pre-Phase-1 agent_run_actions + RFC-0019 permission_rules co-exist
  → upgrade adds Phase-1 cols, leaves permission_rules untouched
  (test_alembic_upgrade_ignores_permission_rules_table)
- both schemas at target shape, no alembic_version
  → stamp 0002, no DDL re-applied to either table
  (test_phase1_complete_with_permission_rules_stamps_head_no_ddl)
- pre-Phase-1 agent_run_actions + permission_rules with real rows
  → migration preserves both tables' data, lands at head
  (test_legacy_db_with_permission_rules_no_phase1_cols)
- legacy sessions table without pending_tool_calls (RFC-0019 silent gap)
  → 0003 adds the column, existing rows preserved with NULL
  (test_legacy_db_with_sessions_table_lacks_pending_tool_calls)
- fresh DB with SessionModel + PermissionRuleModel registered
  → create_all creates pending_tool_calls; 0003 _has_column skips
  (test_fresh_db_pending_tool_calls_already_created_no_re_add)
- Postgres flavour
  → opt-in via NEXAU_PG_TEST_URL env var
  (test_postgres_*)
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from nexau.archs.session.models.agent_run_action_model import AgentRunActionModel
from nexau.archs.session.orm.sql_engine import SQLDatabaseEngine
from nexau.db.migrate import current_revision, upgrade_to_head

# Single source of truth for the alembic head revision. Bump in lock-step
# with the latest file under ``nexau/db/migrations/versions/``. Tests
# below all use this so adding a new migration only touches one line
# here, not 12 hard-coded ``"0002"`` strings scattered through asserts.
HEAD_REVISION = "0003"


def _sync_inspect_columns(sync_url: str, table: str) -> set[str]:
    """Open a sync connection, return the column names of ``table``."""
    eng = create_engine(sync_url)
    try:
        insp = inspect(eng)
        if table not in insp.get_table_names():
            return set()
        return {c["name"] for c in insp.get_columns(table)}
    finally:
        eng.dispose()


def test_fresh_db_setup_models_lands_at_head():
    """Empty file → setup_models creates everything + alembic at head."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "fresh.db"
        async_url = f"sqlite+aiosqlite:///{db_path}"
        sync_url = f"sqlite:///{db_path}"

        async def go():
            eng = SQLDatabaseEngine.from_url(async_url)
            try:
                await eng.setup_models([AgentRunActionModel])
            finally:
                await eng._engine.dispose()

        asyncio.run(go())

        # Schema must include the Phase 1 columns
        cols = _sync_inspect_columns(sync_url, "agent_run_actions")
        assert "idempotency_key" in cols, f"idempotency_key missing, got {cols}"
        assert "extra" in cols, f"extra missing, got {cols}"

        # alembic_version must be at head
        rev = current_revision(async_url)
        assert rev == HEAD_REVISION, f"expected head={HEAD_REVISION}, got {rev}"


def test_legacy_pre_phase1_db_gets_columns_added():
    """Pre-Phase-1 schema (no idempotency_key/extra) → upgrade adds them."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "legacy.db"
        sync_url = f"sqlite:///{db_path}"

        # 1. Create agent_run_actions in pre-Phase-1 shape: minimum cols only.
        eng = create_engine(sync_url)
        try:
            with eng.begin() as conn:
                conn.execute(
                    text("""
                    CREATE TABLE agent_run_actions (
                        action_id VARCHAR(255) NOT NULL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        session_id VARCHAR(255) NOT NULL,
                        agent_id VARCHAR(255) NOT NULL,
                        run_id VARCHAR(255) NOT NULL,
                        root_run_id VARCHAR(255) NOT NULL,
                        parent_run_id VARCHAR(255),
                        agent_name VARCHAR(255) DEFAULT '',
                        created_at_ns BIGINT NOT NULL,
                        action_type VARCHAR(50) NOT NULL,
                        append_messages JSON,
                        replace_messages JSON,
                        undo_before_run_id VARCHAR(255)
                    )
                """)
                )
        finally:
            eng.dispose()

        # Sanity: cols missing
        cols_before = _sync_inspect_columns(sync_url, "agent_run_actions")
        assert "idempotency_key" not in cols_before
        assert "extra" not in cols_before

        # 2. Run upgrade_to_head on this legacy DB
        upgrade_to_head(sync_url)

        # 3. Cols present, revision at head
        cols_after = _sync_inspect_columns(sync_url, "agent_run_actions")
        assert "idempotency_key" in cols_after, f"missing idempotency_key, got {cols_after}"
        assert "extra" in cols_after, f"missing extra, got {cols_after}"
        assert current_revision(sync_url) == HEAD_REVISION


def test_upgrade_is_idempotent_when_at_head():
    """Running upgrade_to_head twice on the same DB is a no-op the second time."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "idem.db"
        async_url = f"sqlite+aiosqlite:///{db_path}"
        sync_url = f"sqlite:///{db_path}"

        async def setup():
            eng = SQLDatabaseEngine.from_url(async_url)
            try:
                await eng.setup_models([AgentRunActionModel])
            finally:
                await eng._engine.dispose()

        asyncio.run(setup())

        rev1 = current_revision(sync_url)
        # Run upgrade a second time
        upgrade_to_head(sync_url)
        rev2 = current_revision(sync_url)
        assert rev1 == rev2 == HEAD_REVISION


def test_auto_migrate_off_verifies_only():
    """NEXAU_AUTO_MIGRATE=off should fail loud on a not-at-head DB."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "verifyonly.db"
        sync_url = f"sqlite:///{db_path}"

        # Create legacy schema (not at head)
        eng = create_engine(sync_url)
        try:
            with eng.begin() as conn:
                conn.execute(
                    text("""
                    CREATE TABLE agent_run_actions (
                        action_id VARCHAR(255) NOT NULL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        session_id VARCHAR(255) NOT NULL,
                        agent_id VARCHAR(255) NOT NULL,
                        run_id VARCHAR(255) NOT NULL,
                        root_run_id VARCHAR(255) NOT NULL,
                        agent_name VARCHAR(255) DEFAULT '',
                        created_at_ns BIGINT NOT NULL,
                        action_type VARCHAR(50) NOT NULL
                    )
                """)
                )
        finally:
            eng.dispose()

        os.environ["NEXAU_AUTO_MIGRATE"] = "off"
        try:
            failed = False
            try:
                upgrade_to_head(sync_url)
            except RuntimeError as exc:
                failed = True
                assert "NEXAU_AUTO_MIGRATE" in str(exc)
            assert failed, "expected RuntimeError when NEXAU_AUTO_MIGRATE=off and DB not at head"
        finally:
            os.environ.pop("NEXAU_AUTO_MIGRATE", None)


# ============================================================================
# Edge-case scenarios — every scenario the NAC + xiaobei + OSS upgrade can
# realistically hit. Adding a new migration step? Add a row to the matrix
# above and a test below; do not modify the existing tests in place.
# ============================================================================


def _create_phase1_complete_table(sync_url: str) -> None:
    """Create agent_run_actions in the post-Phase-1 shape (all cols present).

    Models the NAC scenario where NAC's own migration 0017 created the table
    BEFORE nexau's bundled alembic ever ran. nexau must detect "table already
    matches head" and stamp 0002 without trying to add the columns again.
    """
    eng = create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(
                text("""
                CREATE TABLE agent_run_actions (
                    action_id VARCHAR(255) NOT NULL PRIMARY KEY,
                    user_id VARCHAR(255) NOT NULL,
                    session_id VARCHAR(255) NOT NULL,
                    agent_id VARCHAR(255) NOT NULL,
                    run_id VARCHAR(255) NOT NULL,
                    root_run_id VARCHAR(255) NOT NULL,
                    parent_run_id VARCHAR(255),
                    agent_name VARCHAR(255) DEFAULT '',
                    created_at_ns BIGINT NOT NULL,
                    action_type VARCHAR(50) NOT NULL,
                    append_messages JSON,
                    replace_messages JSON,
                    undo_before_run_id VARCHAR(255),
                    idempotency_key VARCHAR(255),
                    extra JSON
                )
            """)
            )
    finally:
        eng.dispose()


def test_pre_existing_table_with_phase1_cols_stamps_head_no_ddl():
    """NAC pre-existing-table scenario: agent_run_actions exists with all
    Phase-1 cols (NAC migration 0017 created it before nexau alembic ran).

    nexau's _detect_baseline_revision should return "0002" → upgrade just
    stamps, runs zero DDL. The test verifies nothing changed about the
    table structure post-upgrade.
    """
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "nac_preexisting.db"
        sync_url = f"sqlite:///{db_path}"

        _create_phase1_complete_table(sync_url)

        cols_before = _sync_inspect_columns(sync_url, "agent_run_actions")
        assert "idempotency_key" in cols_before
        assert "extra" in cols_before

        # No alembic_version yet — verify
        eng = create_engine(sync_url)
        try:
            insp = inspect(eng)
            assert "alembic_version" not in insp.get_table_names()
        finally:
            eng.dispose()

        upgrade_to_head(sync_url)

        # Post-upgrade: same column set, alembic_version present at head.
        cols_after = _sync_inspect_columns(sync_url, "agent_run_actions")
        assert cols_before == cols_after, (
            f"upgrade should not have changed agent_run_actions columns; before={sorted(cols_before)}, after={sorted(cols_after)}"
        )
        assert current_revision(sync_url) == HEAD_REVISION


def test_partial_migration_only_idempotency_key_completes_cleanly():
    """Interrupted migration / drift: agent_run_actions has idempotency_key
    but not extra (e.g. someone manually added one column, or 0002 crashed
    halfway through on an old nexau version).

    _has_column in 0002.py makes ``op.add_column`` idempotent → upgrade adds
    only the missing column. Verifies the migration is robust to partial state
    rather than blowing up with DuplicateColumn.
    """
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "partial.db"
        sync_url = f"sqlite:///{db_path}"

        eng = create_engine(sync_url)
        try:
            with eng.begin() as conn:
                conn.execute(
                    text("""
                    CREATE TABLE agent_run_actions (
                        action_id VARCHAR(255) NOT NULL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        session_id VARCHAR(255) NOT NULL,
                        agent_id VARCHAR(255) NOT NULL,
                        run_id VARCHAR(255) NOT NULL,
                        root_run_id VARCHAR(255) NOT NULL,
                        agent_name VARCHAR(255) DEFAULT '',
                        created_at_ns BIGINT NOT NULL,
                        action_type VARCHAR(50) NOT NULL,
                        idempotency_key VARCHAR(255)
                    )
                """)
                )
        finally:
            eng.dispose()

        cols_before = _sync_inspect_columns(sync_url, "agent_run_actions")
        assert "idempotency_key" in cols_before
        assert "extra" not in cols_before

        upgrade_to_head(sync_url)

        cols_after = _sync_inspect_columns(sync_url, "agent_run_actions")
        assert "idempotency_key" in cols_after, "idempotency_key should still be there"
        assert "extra" in cols_after, "extra should have been added"
        assert current_revision(sync_url) == HEAD_REVISION


# ----------------------------------------------------------------------------
# RFC-0019 coexistence — main carries tool-permission tables (PR #481) and
# this branch carries RFC-0022 alembic. The same DB needs to host BOTH:
#   - `permission_rules` (created by ``metadata.create_all`` from
#     `nexau/archs/session/models/permission_rule.py`)
#   - alembic-managed `agent_run_actions`
#
# The migrations must:
# 1. Not touch `permission_rules` (alembic only owns agent_run_actions).
# 2. Tolerate `permission_rules` already existing when stamping baseline.
# 3. Tolerate `permission_rules` AND `sessions.pending_tool_calls` JSON column
#    coexisting with a pre-Phase-1 `agent_run_actions` (legacy upgrade path).
#
# The "fresh DB" path uses the real ``PermissionRuleModel``; the legacy paths
# use raw SQL DDL that mirrors what ``metadata.create_all`` would emit for
# the same model on an old prod DB that pre-dates the alembic bundling.
# ----------------------------------------------------------------------------


_PERMISSION_RULES_DDL = """
CREATE TABLE permission_rules (
    user_id      VARCHAR(255) NOT NULL,
    session_id   VARCHAR(255) NOT NULL,
    tool_name    VARCHAR(255) NOT NULL,
    rule_content TEXT NOT NULL,
    behavior     VARCHAR(16) NOT NULL CHECK (behavior IN ('allow', 'deny')),
    source       VARCHAR(16) NOT NULL CHECK (source IN ('config', 'user')),
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, session_id, tool_name, rule_content, behavior)
)
"""

_LEGACY_PRE_PHASE1_AGENT_RUN_ACTIONS_DDL = """
CREATE TABLE agent_run_actions (
    action_id VARCHAR(255) NOT NULL PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    session_id VARCHAR(255) NOT NULL,
    agent_id VARCHAR(255) NOT NULL,
    run_id VARCHAR(255) NOT NULL,
    root_run_id VARCHAR(255) NOT NULL,
    parent_run_id VARCHAR(255),
    agent_name VARCHAR(255) DEFAULT '',
    created_at_ns BIGINT NOT NULL,
    action_type VARCHAR(50) NOT NULL,
    append_messages JSON,
    replace_messages JSON,
    undo_before_run_id VARCHAR(255)
)
"""


def test_setup_models_with_permission_rule_lands_at_head():
    """Fresh DB: register both ``AgentRunActionModel`` AND the real
    ``PermissionRuleModel`` (RFC-0019). ``setup_models`` should create
    both tables via ORM and land alembic at head — NO DDL re-applied to
    permission_rules even though it's adjacent in the metadata.

    This is the post-merge happy path: every new deploy boots with both
    models registered. If the alembic stamp logic ever tries to manage
    permission_rules, this test trips.
    """
    from nexau.archs.session.models.permission_rule import PermissionRuleModel

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "fresh_with_perm.db"
        async_url = f"sqlite+aiosqlite:///{db_path}"
        sync_url = f"sqlite:///{db_path}"

        async def go():
            eng = SQLDatabaseEngine.from_url(async_url)
            try:
                # Both models go through metadata.create_all together;
                # alembic stamp + (no-op) upgrade follows.
                await eng.setup_models([AgentRunActionModel, PermissionRuleModel])
            finally:
                await eng._engine.dispose()

        asyncio.run(go())

        # Both tables exist with the expected columns.
        arr_cols = _sync_inspect_columns(sync_url, "agent_run_actions")
        assert "idempotency_key" in arr_cols
        assert "extra" in arr_cols

        pr_cols = _sync_inspect_columns(sync_url, "permission_rules")
        assert pr_cols == {
            "user_id",
            "session_id",
            "tool_name",
            "rule_content",
            "behavior",
            "source",
            "created_at",
        }, f"permission_rules schema drift: {pr_cols}"

        assert current_revision(sync_url) == HEAD_REVISION


def test_alembic_upgrade_ignores_permission_rules_table():
    """Permission rules table from RFC-0019 must survive alembic upgrade
    untouched — alembic only manages tables it owns (agent_run_actions),
    not tables that landed via ``metadata.create_all`` from other models.

    Failure mode this guards against: a future autogenerate that picks up
    ``permission_rules`` as 'unmanaged' and emits a DROP — would silently
    delete user permission state on next deploy.
    """
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "rfc0019.db"
        sync_url = f"sqlite:///{db_path}"

        # Seed both shapes: pre-Phase-1 agent_run_actions + post-RFC-0019
        # permission_rules with one user-set rule.
        eng = create_engine(sync_url)
        try:
            with eng.begin() as conn:
                conn.execute(text(_LEGACY_PRE_PHASE1_AGENT_RUN_ACTIONS_DDL))
                conn.execute(text(_PERMISSION_RULES_DDL))
                conn.execute(
                    text(
                        "INSERT INTO permission_rules "
                        "(user_id, session_id, tool_name, rule_content, behavior, source) "
                        "VALUES ('u1', 's1', 'shell', '*', 'deny', 'user')"
                    )
                )
        finally:
            eng.dispose()

        # Upgrade: should stamp 0001 (pre-phase1 baseline detected) and run
        # 0002 (add idempotency_key + extra). Should not touch permission_rules.
        upgrade_to_head(sync_url)

        # 1. Phase-1 cols added to agent_run_actions.
        arr_cols = _sync_inspect_columns(sync_url, "agent_run_actions")
        assert "idempotency_key" in arr_cols
        assert "extra" in arr_cols

        # 2. permission_rules schema unchanged + row preserved.
        pr_cols = _sync_inspect_columns(sync_url, "permission_rules")
        assert pr_cols == {
            "user_id",
            "session_id",
            "tool_name",
            "rule_content",
            "behavior",
            "source",
            "created_at",
        }, f"permission_rules schema drift: {pr_cols}"

        eng = create_engine(sync_url)
        try:
            with eng.begin() as conn:
                rows = conn.execute(text("SELECT user_id, tool_name, behavior FROM permission_rules")).fetchall()
                assert rows == [("u1", "shell", "deny")], f"row lost: {rows}"
        finally:
            eng.dispose()

        assert current_revision(sync_url) == HEAD_REVISION


def test_phase1_complete_with_permission_rules_stamps_head_no_ddl():
    """Both RFC-0019 and RFC-0022 schemas already at target shape (e.g. a
    fresh prod DB bootstrapped by post-merge ``metadata.create_all``) but
    no ``alembic_version`` row yet. Upgrade should stamp head and emit no
    DDL on either table.

    Why this matters: existing NAC deployments running pre-bundled-alembic
    nexau already have agent_run_actions at the post-Phase-1 shape AND
    (after deploying main) permission_rules. The first post-#528 deploy
    must be a no-op stamp, not re-run any DDL — re-running 0002's
    ``op.add_column('idempotency_key')`` against a column that already
    exists would error on PG and silently drop on SQLite.
    """
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "stamp.db"
        sync_url = f"sqlite:///{db_path}"

        _create_phase1_complete_table(sync_url)
        eng = create_engine(sync_url)
        try:
            with eng.begin() as conn:
                conn.execute(text(_PERMISSION_RULES_DDL))
        finally:
            eng.dispose()

        # Snapshot column sets before upgrade.
        arr_before = _sync_inspect_columns(sync_url, "agent_run_actions")
        pr_before = _sync_inspect_columns(sync_url, "permission_rules")

        upgrade_to_head(sync_url)

        # No DDL drift on either table.
        assert _sync_inspect_columns(sync_url, "agent_run_actions") == arr_before
        assert _sync_inspect_columns(sync_url, "permission_rules") == pr_before
        assert current_revision(sync_url) == HEAD_REVISION


def test_legacy_db_with_permission_rules_no_phase1_cols():
    """The realistic post-merge legacy upgrade: a long-running prod DB has
    permission_rules created by main's ``metadata.create_all`` (RFC-0019
    deployed first) but agent_run_actions still lacks ``idempotency_key`` /
    ``extra`` (pre-Phase-1, because that DB pre-dates RFC-0022 bundling).

    Upgrade must:
    - detect baseline as 0001 (pre-Phase-1) using agent_run_actions shape,
    - run 0002 to add Phase-1 cols,
    - leave permission_rules alone.

    This is the path NAC's xiaobei deploy will take when #528 lands.
    """
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "legacy_with_perm.db"
        sync_url = f"sqlite:///{db_path}"

        eng = create_engine(sync_url)
        try:
            with eng.begin() as conn:
                conn.execute(text(_LEGACY_PRE_PHASE1_AGENT_RUN_ACTIONS_DDL))
                conn.execute(text(_PERMISSION_RULES_DDL))
                # Realistic load: a few legacy actions + a permission rule.
                for i in range(3):
                    conn.execute(
                        text(
                            "INSERT INTO agent_run_actions "
                            "(action_id, user_id, session_id, agent_id, run_id, root_run_id, "
                            " created_at_ns, action_type) "
                            "VALUES (:aid, 'u', 's', 'a', 'r', 'r', :ts, 'append')"
                        ),
                        {"aid": f"act{i}", "ts": 1000 + i},
                    )
                conn.execute(
                    text(
                        "INSERT INTO permission_rules "
                        "(user_id, session_id, tool_name, rule_content, behavior, source) "
                        "VALUES ('u', 's', 'write_file', '*.py', 'allow', 'config')"
                    )
                )
        finally:
            eng.dispose()

        upgrade_to_head(sync_url)

        # agent_run_actions now at head, legacy rows survived.
        arr_cols = _sync_inspect_columns(sync_url, "agent_run_actions")
        assert "idempotency_key" in arr_cols
        assert "extra" in arr_cols

        eng = create_engine(sync_url)
        try:
            with eng.connect() as conn:
                action_count = conn.execute(text("SELECT COUNT(*) FROM agent_run_actions")).scalar()
                assert action_count == 3, "legacy actions lost during migration"
                rule_count = conn.execute(text("SELECT COUNT(*) FROM permission_rules")).scalar()
                assert rule_count == 1, "permission rule lost during migration"
        finally:
            eng.dispose()

        assert current_revision(sync_url) == HEAD_REVISION


def test_legacy_db_with_sessions_table_lacks_pending_tool_calls():
    """The silent gap from RFC-0019 (PR #481): a long-running prod DB has
    a ``sessions`` table that pre-dates the ``pending_tool_calls`` field.
    ``SQLModel.metadata.create_all`` is CREATE-IF-NOT-EXISTS only — it
    does NOT add new columns to existing tables. The SQL hint file
    ``nexau/archs/session/migrations/001_tool_permission.sql`` is
    documentation only; nothing actually executes the ALTER. So the
    next call to ``session.pending_tool_calls`` raises
    ``OperationalError: no such column``.

    Migration 0003 closes this. After upgrade the legacy ``sessions``
    table must carry the new column without losing any existing rows.
    """
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "legacy_sessions.db"
        sync_url = f"sqlite:///{db_path}"

        eng = create_engine(sync_url)
        try:
            with eng.begin() as conn:
                # Pre-RFC-0019 sessions shape — no pending_tool_calls.
                conn.execute(
                    text("""
                    CREATE TABLE sessions (
                        user_id VARCHAR(255) NOT NULL,
                        session_id VARCHAR(255) NOT NULL,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        context JSON,
                        storage JSON,
                        sandbox_state JSON,
                        root_agent_id VARCHAR(255),
                        PRIMARY KEY (user_id, session_id)
                    )
                """)
                )
                conn.execute(text(_LEGACY_PRE_PHASE1_AGENT_RUN_ACTIONS_DDL))
                # Two real session rows to make sure migration preserves them.
                conn.execute(text("INSERT INTO sessions (user_id, session_id) VALUES ('u1', 's1'), ('u2', 's2')"))
        finally:
            eng.dispose()

        cols_before = _sync_inspect_columns(sync_url, "sessions")
        assert "pending_tool_calls" not in cols_before, f"setup error — col already there: {cols_before}"

        upgrade_to_head(sync_url)

        cols_after = _sync_inspect_columns(sync_url, "sessions")
        assert "pending_tool_calls" in cols_after, f"0003 migration did not add the column: {cols_after}"

        # Existing sessions survived.
        eng = create_engine(sync_url)
        try:
            with eng.connect() as conn:
                count = conn.execute(text("SELECT COUNT(*) FROM sessions")).scalar()
                assert count == 2, "session rows lost during migration"
                # New column reads as NULL on legacy rows.
                vals = conn.execute(text("SELECT pending_tool_calls FROM sessions ORDER BY user_id")).fetchall()
                assert all(v[0] is None for v in vals), f"unexpected default: {vals}"
        finally:
            eng.dispose()

        assert current_revision(sync_url) == HEAD_REVISION


def test_fresh_db_pending_tool_calls_already_created_no_re_add():
    """Fresh DB path: ``setup_models`` runs ``create_all`` first, which
    creates ``sessions`` WITH ``pending_tool_calls`` (since #481 added
    it to the SQLModel). Migration 0003's ``_has_column`` guard must
    detect this and skip the ADD COLUMN — otherwise SQLite raises
    "duplicate column name" and PG raises a similar error.
    """
    from nexau.archs.session.models.permission_rule import PermissionRuleModel
    from nexau.archs.session.models.session import SessionModel

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "fresh_with_perm_session.db"
        async_url = f"sqlite+aiosqlite:///{db_path}"
        sync_url = f"sqlite:///{db_path}"

        async def go():
            eng = SQLDatabaseEngine.from_url(async_url)
            try:
                await eng.setup_models([AgentRunActionModel, PermissionRuleModel, SessionModel])
            finally:
                await eng._engine.dispose()

        asyncio.run(go())

        cols = _sync_inspect_columns(sync_url, "sessions")
        assert "pending_tool_calls" in cols
        assert current_revision(sync_url) == HEAD_REVISION


# ============================================================================
# Upgrade matrix — exercise EVERY realistic DB starting shape end-to-end.
#
# Goal: each scenario constructs a specific pre-existing schema/data state,
# runs ``upgrade_to_head``, and asserts (a) no exception, (b) lands at
# HEAD_REVISION, (c) every required column exists, (d) seed rows survive.
#
# Discipline: when we add a new migration revision, add a new SCENARIO row
# below — do not modify existing scenarios. The matrix is the regression
# net for "all the shapes we've ever seen in prod".
# ============================================================================


def _seed_pre_phase1(conn) -> None:
    """Pre-Phase-1: agent_run_actions without idempotency_key/extra."""
    conn.execute(text(_LEGACY_PRE_PHASE1_AGENT_RUN_ACTIONS_DDL))
    for i in range(2):
        conn.execute(
            text(
                "INSERT INTO agent_run_actions "
                "(action_id, user_id, session_id, agent_id, run_id, root_run_id, "
                " created_at_ns, action_type) "
                "VALUES (:aid, 'u', 's', 'a', 'r', 'r', :ts, 'append')"
            ),
            {"aid": f"act{i}", "ts": 1000 + i},
        )


def _seed_phase1_complete(conn) -> None:
    """Phase-1 complete: agent_run_actions WITH idempotency_key + extra."""
    conn.execute(
        text("""
        CREATE TABLE agent_run_actions (
            action_id VARCHAR(255) NOT NULL PRIMARY KEY,
            user_id VARCHAR(255) NOT NULL,
            session_id VARCHAR(255) NOT NULL,
            agent_id VARCHAR(255) NOT NULL,
            run_id VARCHAR(255) NOT NULL,
            root_run_id VARCHAR(255) NOT NULL,
            parent_run_id VARCHAR(255),
            agent_name VARCHAR(255) DEFAULT '',
            created_at_ns BIGINT NOT NULL,
            action_type VARCHAR(50) NOT NULL,
            append_messages JSON,
            replace_messages JSON,
            undo_before_run_id VARCHAR(255),
            idempotency_key VARCHAR(255),
            extra JSON
        )
    """)
    )


def _seed_phase1_partial_only_idempotency(conn) -> None:
    """Half-applied Phase-1: only idempotency_key, no extra."""
    conn.execute(
        text("""
        CREATE TABLE agent_run_actions (
            action_id VARCHAR(255) NOT NULL PRIMARY KEY,
            user_id VARCHAR(255) NOT NULL,
            session_id VARCHAR(255) NOT NULL,
            agent_id VARCHAR(255) NOT NULL,
            run_id VARCHAR(255) NOT NULL,
            root_run_id VARCHAR(255) NOT NULL,
            agent_name VARCHAR(255) DEFAULT '',
            created_at_ns BIGINT NOT NULL,
            action_type VARCHAR(50) NOT NULL,
            idempotency_key VARCHAR(255)
        )
    """)
    )


def _seed_legacy_sessions(conn) -> None:
    """Pre-RFC-0019 sessions (no pending_tool_calls)."""
    conn.execute(
        text("""
        CREATE TABLE sessions (
            user_id VARCHAR(255) NOT NULL,
            session_id VARCHAR(255) NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            context JSON,
            storage JSON,
            sandbox_state JSON,
            root_agent_id VARCHAR(255),
            PRIMARY KEY (user_id, session_id)
        )
    """)
    )
    conn.execute(text("INSERT INTO sessions (user_id, session_id) VALUES ('u', 's')"))


def _seed_post_rfc0019_sessions(conn) -> None:
    """Post-RFC-0019 sessions (already has pending_tool_calls col)."""
    conn.execute(
        text("""
        CREATE TABLE sessions (
            user_id VARCHAR(255) NOT NULL,
            session_id VARCHAR(255) NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            context JSON,
            storage JSON,
            sandbox_state JSON,
            root_agent_id VARCHAR(255),
            pending_tool_calls JSON,
            PRIMARY KEY (user_id, session_id)
        )
    """)
    )


def _seed_permission_rules(conn) -> None:
    conn.execute(text(_PERMISSION_RULES_DDL))
    conn.execute(
        text(
            "INSERT INTO permission_rules "
            "(user_id, session_id, tool_name, rule_content, behavior, source) "
            "VALUES ('u', 's', 'shell', '*', 'deny', 'user')"
        )
    )


def _seed_alembic_version(rev: str):
    """Closure that stamps ``alembic_version`` to a specific revision."""

    def go(conn):
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
        conn.execute(text("INSERT INTO alembic_version VALUES (:r)"), {"r": rev})

    return go


# (label, [list of seed builders to apply in order]) — each builder takes a
# transactional connection and emits DDL + optional seed rows.
#
# Every scenario must include ``agent_run_actions`` (in some shape) — it's
# the table the migrations target. The pure-empty-DB path is realistic
# only via ``setup_models`` and is covered separately by
# ``test_fresh_db_setup_models_lands_at_head``.
_UPGRADE_MATRIX: list[tuple[str, list]] = [
    ("pre_phase1_only", [_seed_pre_phase1]),
    ("phase1_complete_no_alembic", [_seed_phase1_complete]),
    ("phase1_partial_only_idempotency_key", [_seed_phase1_partial_only_idempotency]),
    ("pre_phase1_with_permission_rules", [_seed_pre_phase1, _seed_permission_rules]),
    ("phase1_complete_with_permission_rules", [_seed_phase1_complete, _seed_permission_rules]),
    ("pre_phase1_with_legacy_sessions", [_seed_pre_phase1, _seed_legacy_sessions]),
    (
        "pre_phase1_with_post_rfc0019_sessions",
        [_seed_pre_phase1, _seed_post_rfc0019_sessions],
    ),
    (
        "everything_legacy",
        [_seed_pre_phase1, _seed_legacy_sessions, _seed_permission_rules],
    ),
    (
        "everything_post_rfc0019_no_alembic",
        [_seed_phase1_complete, _seed_post_rfc0019_sessions, _seed_permission_rules],
    ),
    # Explicit alembic stamps mid-chain — verify upgrade resumes correctly.
    (
        "stamped_at_0001_pre_phase1",
        [_seed_pre_phase1, _seed_alembic_version("0001")],
    ),
    (
        "stamped_at_0002_legacy_sessions",
        [_seed_phase1_complete, _seed_legacy_sessions, _seed_alembic_version("0002")],
    ),
    (
        "stamped_at_0002_no_sessions_at_all",
        [_seed_phase1_complete, _seed_alembic_version("0002")],
    ),
    (
        "stamped_at_0002_post_rfc0019_sessions_already",
        [_seed_phase1_complete, _seed_post_rfc0019_sessions, _seed_alembic_version("0002")],
    ),
]


@pytest.mark.parametrize("label,seed_fns", _UPGRADE_MATRIX, ids=[s[0] for s in _UPGRADE_MATRIX])
def test_upgrade_matrix_lands_at_head(label, seed_fns):
    """For every realistic DB starting shape: ``upgrade_to_head`` must
    succeed without raising and leave the DB at HEAD_REVISION.

    Beyond head-revision, this also asserts:
    - tables that were seeded are still there afterwards
    - any rows we inserted survived the migration
    - ``agent_run_actions`` (if present) has the post-Phase-1 columns
    - ``sessions`` (if present) has ``pending_tool_calls``
    """
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / f"{label}.db"
        sync_url = f"sqlite:///{db_path}"

        # 1. Seed.
        eng = create_engine(sync_url)
        try:
            with eng.begin() as conn:
                for fn in seed_fns:
                    fn(conn)
            # Snapshot row counts for tables we care about.
            with eng.connect() as conn:
                seeded_tables = set(inspect(eng).get_table_names())
                row_counts = {
                    t: conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() or 0 for t in seeded_tables if t != "alembic_version"
                }
        finally:
            eng.dispose()

        # 2. Upgrade — must not raise.
        upgrade_to_head(sync_url)

        # 3. End state: at head.
        assert current_revision(sync_url) == HEAD_REVISION, f"[{label}] revision drift after first upgrade: {current_revision(sync_url)}"

        # 3b. Idempotency — second upgrade must be a no-op (deploys often
        # re-run setup_models on container restart; we cannot have any
        # migration step that rejects an already-at-head DB).
        upgrade_to_head(sync_url)
        assert current_revision(sync_url) == HEAD_REVISION, f"[{label}] revision drift after second upgrade: {current_revision(sync_url)}"

        # 4. Required schema invariants.
        if "agent_run_actions" in seeded_tables:
            arr = _sync_inspect_columns(sync_url, "agent_run_actions")
            assert {"idempotency_key", "extra"}.issubset(arr), f"[{label}] agent_run_actions missing Phase-1 cols: {arr}"
        if "sessions" in seeded_tables:
            sess = _sync_inspect_columns(sync_url, "sessions")
            assert "pending_tool_calls" in sess, f"[{label}] sessions missing pending_tool_calls: {sess}"

        # 5. Row preservation.
        eng = create_engine(sync_url)
        try:
            with eng.connect() as conn:
                for table, expected in row_counts.items():
                    actual = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0
                    assert actual == expected, f"[{label}] {table} row count drift: {expected} → {actual}"
        finally:
            eng.dispose()


def test_foreign_alembic_version_revision_fails_loud():
    """Library-sharing footgun: alembic_version exists but contains a
    revision id that nexau doesn't know about (caller is sharing the DB
    with another alembic-managed project, or migrated from a fork).

    Upgrade must fail loud rather than silently stamp over the foreign id —
    overwriting it would corrupt the OTHER project's migration state.
    """
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "foreign.db"
        sync_url = f"sqlite:///{db_path}"

        # Empty schema + foreign alembic_version row.
        eng = create_engine(sync_url)
        try:
            with eng.begin() as conn:
                conn.execute(
                    text("""
                    CREATE TABLE alembic_version (
                        version_num VARCHAR(32) NOT NULL PRIMARY KEY
                    )
                """)
                )
                conn.execute(text("INSERT INTO alembic_version VALUES ('zzz_unknown_proj')"))
        finally:
            eng.dispose()

        # Upgrade should raise — alembic can't find revision zzz_unknown_proj
        # in its script chain, so command.upgrade fails loudly.
        with pytest.raises(Exception) as excinfo:
            upgrade_to_head(sync_url)
        # Ensure the error message references the unknown revision; otherwise
        # a future refactor could swallow this and we'd never notice.
        msg = str(excinfo.value).lower()
        assert "zzz_unknown_proj" in msg or "revision" in msg or "no such" in msg, f"unexpected error: {excinfo.value!r}"


# ============================================================================
# Coverage gap closers — branches that the lifecycle tests above don't
# naturally hit:
#  - downgrade() for each migration (3 functions, ~16 lines net)
#  - _detect_baseline_revision edge cases (empty / alembic_version present /
#    foreign tables only)
#  - upgrade_to_head no-op path on an already-at-head DB (NEXAU_AUTO_MIGRATE
#    early-return + verify-only success path)
# ============================================================================


def _run_downgrade_one_step(sync_url: str) -> None:
    """Step alembic back exactly one revision. Used to exercise downgrade()."""
    from alembic import command

    from nexau.db.migrate import _build_config

    cfg = _build_config(sync_url)
    command.downgrade(cfg, "-1")


# Downgrade tests run on Postgres only (see _PG bottom section). SQLite's
# ALTER TABLE DROP COLUMN needs batch mode and the migrations target PG;
# downgrade() is a prod-rollback path so PG is the realistic surface.


def test_detect_baseline_returns_none_for_alembic_version_present():
    """Branch: alembic_version table exists → caller skips stamp."""
    from nexau.db.migrate import _detect_baseline_revision

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "with_alembic.db"
        sync_url = f"sqlite:///{db_path}"

        # Manually create just the alembic_version table — simulates a DB that
        # alembic has already touched (no schema yet).
        eng = create_engine(sync_url)
        try:
            with eng.begin() as conn:
                conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
            with eng.connect() as conn:
                assert _detect_baseline_revision(conn) is None
        finally:
            eng.dispose()


def test_detect_baseline_returns_none_for_foreign_tables_only():
    """Branch: tables present but none match nexau fingerprints → start from scratch."""
    from nexau.db.migrate import _detect_baseline_revision

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "foreign.db"
        sync_url = f"sqlite:///{db_path}"

        eng = create_engine(sync_url)
        try:
            with eng.begin() as conn:
                conn.execute(text("CREATE TABLE some_other_app_table (id INTEGER PRIMARY KEY)"))
            with eng.connect() as conn:
                assert _detect_baseline_revision(conn) is None
        finally:
            eng.dispose()


def test_auto_migrate_off_succeeds_when_db_at_head():
    """NEXAU_AUTO_MIGRATE=off + DB already at head → quiet success."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "athead.db"
        async_url = f"sqlite+aiosqlite:///{db_path}"
        sync_url = f"sqlite:///{db_path}"

        async def go():
            eng = SQLDatabaseEngine.from_url(async_url)
            try:
                await eng.setup_models([AgentRunActionModel])
            finally:
                await eng._engine.dispose()

        asyncio.run(go())
        assert current_revision(async_url) == HEAD_REVISION

        os.environ["NEXAU_AUTO_MIGRATE"] = "off"
        try:
            upgrade_to_head(sync_url)  # must not raise
            assert current_revision(async_url) == HEAD_REVISION
        finally:
            os.environ.pop("NEXAU_AUTO_MIGRATE", None)


def test_alembic_offline_mode_emits_sql_via_env_var():
    """Cover env.py ``run_migrations_offline`` + ``NEXAU_ALEMBIC_DATABASE_URL``
    fallback + ``context.is_offline_mode()`` branch.

    Standalone CLI invocation pattern: developer runs
    ``alembic upgrade --sql`` to inspect the SQL that would be applied
    without touching a real DB. The env vars + offline-mode branch in
    nexau/db/migrations/env.py are otherwise unreachable from setup_models /
    upgrade_to_head call paths (which always pass a real connection).

    Targets only revision ``0001`` because later revisions use
    ``_has_column`` introspection guards that need a live connection
    (alembic's MockConnection in offline mode doesn't support inspect()).
    Going as far as 0001 is enough to exercise env.py's offline branch.
    """
    import io
    from contextlib import redirect_stdout

    from alembic import command

    from nexau.db.migrate import _build_config

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "offline.db"
        sync_url = f"sqlite:///{db_path}"

        # NEXAU_ALEMBIC_DATABASE_URL is the env-var fallback env.py reads when
        # the alembic ini lacks a sqlalchemy.url. Setting it (alongside the
        # explicit cfg URL via _build_config) exercises the env-var
        # codepath defensively.
        os.environ["NEXAU_ALEMBIC_DATABASE_URL"] = sync_url
        try:
            cfg = _build_config(sync_url)
            buf = io.StringIO()
            with redirect_stdout(buf):
                # ``sql=True`` flips alembic to offline mode: no DB connection,
                # SQL DDL emitted to stdout. Revision 0001 is the no-op
                # baseline marker — alembic still routes through env.py's
                # offline branch + writes the version-table marker SQL.
                command.upgrade(cfg, "0001", sql=True)
            sql_output = buf.getvalue()
            # 0001 is a no-op revision but alembic emits the
            # alembic_version stamp/insert SQL on every offline run.
            assert "alembic_version" in sql_output, f"expected version-table SQL, got: {sql_output[:300]}"
            assert "0001" in sql_output
        finally:
            os.environ.pop("NEXAU_ALEMBIC_DATABASE_URL", None)


def test_detect_baseline_returns_none_for_truly_empty_db():
    """Cover migrate.py:132 — ``if not tables: return None`` branch.

    Existing tests seed at least one table before invoking
    ``_detect_baseline_revision`` (alembic_version, foreign tables, etc).
    A truly empty DB takes the early-return branch first.
    """
    from nexau.db.migrate import _detect_baseline_revision

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "empty.db"
        sync_url = f"sqlite:///{db_path}"

        # Touch the file (SQLite needs to exist for create_engine to attach)
        # but DON'T create any tables.
        eng = create_engine(sync_url)
        try:
            with eng.connect() as conn:
                # No tables → empty DB → early return None
                assert _detect_baseline_revision(conn) is None
        finally:
            eng.dispose()


# ============================================================================
# Postgres flavor — opt-in. Set NEXAU_PG_TEST_URL to a sync Postgres URL
# (e.g. postgresql+psycopg2://user:pass@localhost:5432/test_db). Skipped by
# default so dev machines without PG don't fail the suite.
#
# Why these matter even though the SQLite tests cover the logic: the alembic
# DDL ops (op.add_column with index, partial unique index WHERE clauses) take
# different code paths on PG. SQLite passes them as no-ops in some cases.
# ============================================================================


PG_URL = os.environ.get("NEXAU_PG_TEST_URL")
PG_SKIP_REASON = (
    "Postgres tests require NEXAU_PG_TEST_URL=postgresql+psycopg2://... and a reachable Postgres instance with psycopg installed."
)


def _pg_drop_test_tables(sync_url: str) -> None:
    """Drop nexau-managed tables + alembic_version so each test starts clean."""
    eng = create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS agent_run_actions CASCADE"))
            conn.execute(text("DROP TABLE IF EXISTS alembic_version CASCADE"))
    finally:
        eng.dispose()


@pytest.mark.skipif(PG_URL is None, reason=PG_SKIP_REASON)
def test_postgres_fresh_db_setup_models_lands_at_head():
    """Postgres equivalent of test_fresh_db_setup_models_lands_at_head."""
    assert PG_URL is not None  # narrow for type checker
    pytest.importorskip("psycopg2")
    _pg_drop_test_tables(PG_URL)
    try:
        # The async URL nexau hands to setup_models maps via _to_sync_url.
        async_url = PG_URL.replace("psycopg2", "asyncpg")

        async def go():
            eng = SQLDatabaseEngine.from_url(async_url)
            try:
                await eng.setup_models([AgentRunActionModel])
            finally:
                await eng._engine.dispose()

        asyncio.run(go())

        cols = _sync_inspect_columns(PG_URL, "agent_run_actions")
        assert "idempotency_key" in cols
        assert "extra" in cols
        assert current_revision(PG_URL) == HEAD_REVISION
    finally:
        _pg_drop_test_tables(PG_URL)


@pytest.mark.skipif(PG_URL is None, reason=PG_SKIP_REASON)
def test_postgres_legacy_db_with_permission_rules_no_phase1_cols():
    """Postgres equivalent of test_legacy_db_with_permission_rules_no_phase1_cols.

    Critical because this is the real path xiaobei (prod, PG-backed) takes
    when #528 lands after main's RFC-0019 merge. SQLite tolerates a lot
    of schema-coexistence quirks PG enforces strictly (e.g. CHECK
    constraints, JSONB column type, PG-specific catalog scans during
    alembic baseline detection).
    """
    assert PG_URL is not None
    pytest.importorskip("psycopg2")
    _pg_drop_test_tables(PG_URL)
    eng = create_engine(PG_URL)
    try:
        with eng.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS permission_rules CASCADE"))
            conn.execute(text(_LEGACY_PRE_PHASE1_AGENT_RUN_ACTIONS_DDL.replace("JSON,", "JSONB,")))
            conn.execute(text(_PERMISSION_RULES_DDL))
            conn.execute(
                text(
                    "INSERT INTO permission_rules "
                    "(user_id, session_id, tool_name, rule_content, behavior, source) "
                    "VALUES ('u', 's', 'shell', '*', 'deny', 'user')"
                )
            )
    finally:
        eng.dispose()
    try:
        upgrade_to_head(PG_URL)

        arr_cols = _sync_inspect_columns(PG_URL, "agent_run_actions")
        assert "idempotency_key" in arr_cols
        assert "extra" in arr_cols
        pr_cols = _sync_inspect_columns(PG_URL, "permission_rules")
        assert pr_cols == {
            "user_id",
            "session_id",
            "tool_name",
            "rule_content",
            "behavior",
            "source",
            "created_at",
        }
        assert current_revision(PG_URL) == HEAD_REVISION
    finally:
        eng = create_engine(PG_URL)
        try:
            with eng.begin() as conn:
                conn.execute(text("DROP TABLE IF EXISTS permission_rules CASCADE"))
        finally:
            eng.dispose()
        _pg_drop_test_tables(PG_URL)


@pytest.mark.skipif(PG_URL is None, reason=PG_SKIP_REASON)
def test_postgres_legacy_pre_phase1_db_gets_columns_added():
    """Postgres equivalent of test_legacy_pre_phase1_db_gets_columns_added.

    Critical because PG enforces partial unique index semantics differently
    than SQLite (PG honors the WHERE clause; SQLite ignores it but enforces
    uniqueness over indexed cols). If 0002 silently produces an unintended
    full-uniqueness index on PG, multi-NULL inserts would fail.
    """
    assert PG_URL is not None
    pytest.importorskip("psycopg2")
    _pg_drop_test_tables(PG_URL)
    try:
        eng = create_engine(PG_URL)
        try:
            with eng.begin() as conn:
                conn.execute(
                    text("""
                    CREATE TABLE agent_run_actions (
                        action_id VARCHAR(255) NOT NULL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        session_id VARCHAR(255) NOT NULL,
                        agent_id VARCHAR(255) NOT NULL,
                        run_id VARCHAR(255) NOT NULL,
                        root_run_id VARCHAR(255) NOT NULL,
                        parent_run_id VARCHAR(255),
                        agent_name VARCHAR(255) DEFAULT '',
                        created_at_ns BIGINT NOT NULL,
                        action_type VARCHAR(50) NOT NULL,
                        append_messages JSONB,
                        replace_messages JSONB,
                        undo_before_run_id VARCHAR(255)
                    )
                """)
                )
        finally:
            eng.dispose()

        upgrade_to_head(PG_URL)

        cols = _sync_inspect_columns(PG_URL, "agent_run_actions")
        assert "idempotency_key" in cols
        assert "extra" in cols
        assert current_revision(PG_URL) == HEAD_REVISION

        # Verify the partial unique index is actually partial: two NULL
        # idempotency_key inserts must succeed.
        eng = create_engine(PG_URL)
        try:
            with eng.begin() as conn:
                for i in range(2):
                    conn.execute(
                        text(
                            "INSERT INTO agent_run_actions "
                            "(action_id, user_id, session_id, agent_id, run_id, root_run_id, "
                            " created_at_ns, action_type) "
                            "VALUES (:aid, 'u', 's', 'a', 'r', 'r', :ts, 'append')"
                        ),
                        {"aid": f"a{i}", "ts": 1000 + i},
                    )
        finally:
            eng.dispose()
    finally:
        _pg_drop_test_tables(PG_URL)


# ============================================================================
# PG-only downgrade coverage. Migrations target Postgres in prod; downgrade
# is the rollback path. SQLite can't execute these because DROP COLUMN /
# partial-index drops need batch mode that the migration files don't wrap.
# ============================================================================


def _pg_drop_session_tables(sync_url: str) -> None:
    eng = create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS sessions CASCADE"))
            conn.execute(text("DROP TABLE IF EXISTS permission_rules CASCADE"))
    finally:
        eng.dispose()


def _pg_bootstrap_at_head(models: list) -> None:
    """Create tables + stamp alembic to head — the path real callers take.

    ``upgrade_to_head`` alone only stamps a stamped revision on a stamped
    DB; it does NOT issue CREATE TABLE for fresh schemas. setup_models
    runs ``SQLModel.metadata.create_all`` (which creates the tables) and
    then ``upgrade_to_head`` (which detects fingerprint and stamps).
    """
    assert PG_URL is not None
    async_url = PG_URL.replace("psycopg2", "asyncpg")

    async def go():
        eng = SQLDatabaseEngine.from_url(async_url)
        try:
            await eng.setup_models(models)
        finally:
            await eng._engine.dispose()

    asyncio.run(go())


@pytest.mark.skipif(PG_URL is None, reason=PG_SKIP_REASON)
def test_postgres_downgrade_phase1_drops_idempotency_key_and_extra():
    """0002 downgrade on PG actually drops the index + columns."""
    assert PG_URL is not None
    pytest.importorskip("psycopg2")
    _pg_drop_test_tables(PG_URL)
    try:
        _pg_bootstrap_at_head([AgentRunActionModel])
        assert {"idempotency_key", "extra"}.issubset(_sync_inspect_columns(PG_URL, "agent_run_actions"))

        _run_downgrade_one_step(PG_URL)  # 0003 → 0002 (no-op for agent_run_actions)
        _run_downgrade_one_step(PG_URL)  # 0002 → 0001

        cols = _sync_inspect_columns(PG_URL, "agent_run_actions")
        assert "idempotency_key" not in cols, f"idempotency_key not dropped, got {cols}"
        assert "extra" not in cols, f"extra not dropped, got {cols}"
        assert current_revision(PG_URL) == "0001"
    finally:
        _pg_drop_test_tables(PG_URL)


@pytest.mark.skipif(PG_URL is None, reason=PG_SKIP_REASON)
def test_postgres_downgrade_pending_tool_calls():
    """0003 downgrade drops sessions.pending_tool_calls when the table exists."""
    from nexau.archs.session.models.session import SessionModel

    assert PG_URL is not None
    pytest.importorskip("psycopg2")
    _pg_drop_test_tables(PG_URL)
    _pg_drop_session_tables(PG_URL)
    try:
        # setup_models creates BOTH tables + stamps to head, then 0003 has
        # actually executed against the existing sessions table — that's
        # what we're about to roll back.
        _pg_bootstrap_at_head([AgentRunActionModel, SessionModel])
        assert "pending_tool_calls" in _sync_inspect_columns(PG_URL, "sessions")

        _run_downgrade_one_step(PG_URL)  # 0003 → 0002
        cols = _sync_inspect_columns(PG_URL, "sessions")
        assert "pending_tool_calls" not in cols, f"pending_tool_calls not dropped, got {cols}"
        assert current_revision(PG_URL) == "0002"
    finally:
        _pg_drop_session_tables(PG_URL)
        _pg_drop_test_tables(PG_URL)


# Note: there is intentionally no "downgrade past 0001 drops the table"
# test. 0001 is a no-op baseline marker — see nexau/db/migrations/versions/
# 0001_initial.py docstring. The agent_run_actions table is created by
# SQLModel.metadata.create_all (not alembic), so stamping back to base
# leaves the table intact. The 0002 + 0003 downgrade tests above cover
# every reverse-DDL we actually own.
