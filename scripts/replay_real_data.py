#!/usr/bin/env python
# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""CLI: validate ``load_messages`` correctness against a real sqlite dump.

Usage:
    uv run python scripts/replay_real_data.py /path/to/dump.db

What it does
------------
For every (user_id, session_id, agent_id) in the DB, runs the production
``AgentRunActionService.load_messages`` AND an independent oracle that reads
raw JSON via stdlib (no SQLAlchemy ORM, no Pydantic Message). Compares
``(id, role, text_chars, block_count, sorted_block_types)`` per message,
position-sensitive. Reports oracle parity, errors, and cross-key
message-id reuse classified as same-content (legitimate fork/migration)
vs different-content (real bug).

Privacy
-------
Never logs message content. Output is counts, structure indices, field
names, and id prefixes only.

Schema requirements
-------------------
Most prod dumps come from before Phase 1 added ``idempotency_key`` /
``extra`` columns. The script auto-adds them if missing (NULL-fillable).

Exit codes
----------
0  — all clean (oracle parity 100% AND zero different-content reuse)
1  — at least one mismatch / error / different-content reuse
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

# Make ``tests.integration.replay_oracle`` importable regardless of cwd.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from tests.integration.replay_oracle import run_replay_sync  # noqa: E402


def _ensure_phase1_columns(db_path: Path) -> None:
    """Add ``idempotency_key`` / ``extra`` columns if absent (idempotent)."""
    conn = sqlite3.connect(db_path)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(agent_run_actions)").fetchall()}
        if "idempotency_key" not in cols:
            conn.execute("ALTER TABLE agent_run_actions ADD COLUMN idempotency_key VARCHAR")
        if "extra" not in cols:
            conn.execute("ALTER TABLE agent_run_actions ADD COLUMN extra JSON")
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path", type=Path, help="Path to sqlite dump containing agent_run_actions table")
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Modify the input DB file directly (default: work on a temp copy)",
    )
    args = parser.parse_args()

    if not args.db_path.exists():
        print(f"error: {args.db_path} does not exist", file=sys.stderr)
        return 2

    if args.in_place:
        target = args.db_path
    else:
        # Copy to /tmp so we can ALTER without touching the original
        tmp = tempfile.NamedTemporaryFile(suffix=".db", prefix="replay_", delete=False)
        tmp.close()
        shutil.copy2(args.db_path, tmp.name)
        target = Path(tmp.name)

    try:
        _ensure_phase1_columns(target)
        result = run_replay_sync(str(target))

        print(f"Source: {args.db_path}")
        print(f"Sessions analyzed: {result.total_sessions}")
        print()
        print(result.summarize())

        if result.mismatches:
            print(f"\nFirst {min(5, len(result.mismatches))} mismatches:")
            for m in result.mismatches[:5]:
                print(m)

        if result.errors:
            print(f"\nFirst {min(5, len(result.errors))} errors:")
            for e in result.errors[:5]:
                print(e)

        if result.diff_content_reuses > 0:
            print("\n*** DIFFERENT-CONTENT cross-key reuse — real bugs to investigate ***")
            for mid, keys, content_sigs in result.diff_content_samples:
                user_count = len({k[0] for k in keys})
                print(f"  id={mid[:12]}... in {len(keys)} keys / {user_count} users / {len(content_sigs)} distinct content sigs")

        if result.all_clean:
            print("\nALL CLEAN")
            return 0
        else:
            print("\nDIRTY — investigate before shipping")
            return 1
    finally:
        if not args.in_place and target.exists():
            target.unlink()


if __name__ == "__main__":
    sys.exit(main())
