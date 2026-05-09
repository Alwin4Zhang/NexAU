# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""nexau database migration support (RFC-0022).

Public API for nexau's session-DB schema migration:

- :func:`upgrade_to_head` — apply all pending migrations on a database URL.
  Called automatically from
  ``nexau.archs.session.orm.sql_engine.SQLDatabaseEngine.setup_models``
  unless ``NEXAU_AUTO_MIGRATE=off`` is set in the environment.

- :func:`current_revision` — the alembic revision the database is at.

The migration scripts themselves live under ``nexau/db/migrations/versions/``
and are packaged with the wheel so that ``pip install -U nexau`` is the
only thing a user needs to do — there is no separate ``alembic upgrade``
command for SDK users.
"""

from nexau.db.migrate import current_revision, upgrade_to_head

__all__ = ["current_revision", "upgrade_to_head"]
