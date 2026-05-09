# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Strict replay oracle — validates ``load_messages`` against an independent
implementation that bypasses SQLAlchemy ORM and Pydantic Message entirely.

Why this exists
---------------
The earlier "shadow fold" in tests (``_canonical_fold``) shared the same
SQLAlchemy + Pydantic stack as production, so any schema-misinterpretation
bug (block dropped, type renamed, field type widened) would manifest in
both sides identically and the oracle would silently agree with production.

This module provides:

- ``oracle_fold(db_path, key)``: forward-fold all actions for the key using
  raw ``sqlite3`` + ``json.loads``. Zero ORM, zero Pydantic. Returns plain
  dicts. If production loses any field at the schema layer, the oracle's
  view diverges.
- ``strict_signature(...)``: fingerprint a list of messages or message dicts
  as ``(id, role, text_chars, block_count, sorted_block_types)``. Same
  algorithm regardless of input shape — privacy-safe (never logs content).
- ``classify_cross_key_reuse(...)``: detect message ids appearing under more
  than one ``(user_id, session_id, agent_id)`` key, and split into
  same-content (likely fork/migration, legitimate) vs different-content
  (real bug — dedup error or accidental id reuse).

Used by:
- ``tests/integration/test_real_data_replay.py``: synthetic fixtures + CI.
- ``scripts/replay_real_data.py``: CLI for ops to validate prod dumps.

This module is named without the ``test_`` prefix so pytest doesn't try to
collect it as a test file. Treat it as a private test helper.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from nexau.archs.session.agent_run_action_service import AgentRunActionKey, AgentRunActionService
from nexau.archs.session.models.agent_run_action_model import AgentRunActionModel
from nexau.archs.session.orm.filters import AndFilter
from nexau.archs.session.orm.sql_engine import SQLDatabaseEngine

# (id, role, text_chars, block_count, sorted_block_types_tuple)
StrictSignature = tuple[str, str, int, int, tuple[str, ...]]
# (user_id, session_id, agent_id)
SessionKey = tuple[str, str, str]


# ============================================================================
# Strict signature — privacy-safe fingerprint
# ============================================================================


def _block_signature(content_list: Any) -> tuple[int, int, tuple[str, ...]]:
    """Fingerprint a list of block dicts as (text_chars, count, types).

    text_chars: sum of ``len(block["text"])`` over any block that has a
                string ``text`` field. Block-type-agnostic on purpose, so
                ``TextBlock``, ``ReasoningBlock`` and any other block with
                a text-like attribute all contribute. Same convention used
                on both production-derived and oracle-derived inputs ⟹
                comparable.
    count: total number of blocks (catches "block dropped" bugs).
    types: sorted tuple of block ``type`` strings (catches type renames or
           Pydantic discriminated-union failures that would land a block
           in the wrong subclass).
    """
    if not isinstance(content_list, list):
        return (0, 0, ())
    text_chars = 0
    types: list[str] = []
    for block in content_list:
        if not isinstance(block, dict):
            continue
        t = block.get("text")
        if isinstance(t, str):
            text_chars += len(t)
        types.append(str(block.get("type", "?")))
    return (text_chars, len(content_list), tuple(sorted(types)))


def strict_signature(msgs: Iterable[Any]) -> list[StrictSignature]:
    """Fingerprint each message in ``msgs`` to a strict tuple.

    Accepts either Pydantic ``Message`` instances (uses ``model_dump``) or
    raw dict messages (used by the oracle path). Both go through the SAME
    ``_block_signature`` algorithm so the comparison is meaningful.
    """

    def _role_str(v: Any) -> str:
        # Normalize Role enum / str / dict-after-model_dump to canonical str value
        return v.value if hasattr(v, "value") else str(v)

    out: list[StrictSignature] = []
    for m in msgs:
        if hasattr(m, "model_dump"):
            mid = str(getattr(m, "id"))
            role = _role_str(getattr(m, "role"))
            # Use mode='json' so nested types (Role enum, datetime) serialize the
            # same way the oracle sees them in raw JSONB.
            content_dicts = [b.model_dump(mode="json") if hasattr(b, "model_dump") else b for b in m.content]
        elif isinstance(m, dict):
            mid = str(m.get("id"))
            role = _role_str(m.get("role"))
            content_dicts = m.get("content") or []
        else:
            raise TypeError(f"strict_signature: unsupported message type {type(m).__name__}")
        text_chars, n_blocks, types = _block_signature(content_dicts)
        out.append((mid, role, text_chars, n_blocks, types))
    return out


# ============================================================================
# Oracle fold — raw sqlite3, no ORM/Pydantic
# ============================================================================


def oracle_fold(db_path: str, key: SessionKey) -> list[dict]:
    """Forward-fold ALL actions for ``key`` using raw sqlite + json.loads.

    Algorithm:
      ASC scan by (created_at_ns, action_id):
        REPLACE → state = list(replace_messages); reset dedup
        APPEND  → extend state, dedup by message.id (latest wins, slot preserved)
        UNDO / RUN_START / RUN_END → ignored (UNDO not yet seen in real data;
          for synthetic test coverage see ``oracle_fold_with_undo`` if extended)

    Returns list of dict (raw JSON-decoded messages, SYSTEM filtered).
    """
    user_id, session_id, agent_id = key
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT action_type, append_messages, replace_messages, undo_before_run_id, "
            "       run_id, created_at_ns, action_id "
            "FROM agent_run_actions "
            "WHERE user_id=? AND session_id=? AND agent_id=? "
            "ORDER BY created_at_ns ASC, action_id ASC",
            (user_id, session_id, agent_id),
        ).fetchall()
    finally:
        conn.close()

    state: list[dict] = []
    seen: dict[str, int] = {}

    def apply(msgs: list[dict] | None) -> None:
        if not msgs:
            return
        for m in msgs:
            mid = str(m.get("id"))
            if mid in seen:
                state[seen[mid]] = m
            else:
                seen[mid] = len(state)
                state.append(m)

    # Two-pass: first compute UNDO cutoffs (each undo's target run's first ns),
    # then apply only actions with ns < cutoff_ns.
    # Mirrors production load_messages cutoff_ns semantics.
    cutoffs: list[int] = []
    first_ns_by_run: dict[str, int] = {}
    for _, _, _, _, run_id, ns, _ in rows:
        first_ns_by_run.setdefault(run_id, int(ns))
    for action_type, _, _, undo_target, _, _, _ in rows:
        if action_type == "undo" and undo_target:
            target_first = first_ns_by_run.get(undo_target)
            if target_first is not None:
                cutoffs.append(target_first)
    cutoff_ns = min(cutoffs) if cutoffs else None

    for action_type, app_json, repl_json, _, _, ns, _ in rows:
        if cutoff_ns is not None and int(ns) >= cutoff_ns:
            continue  # undone
        if action_type == "replace":
            state, seen = [], {}
            if repl_json:
                apply(json.loads(repl_json))
        elif action_type == "append":
            if app_json:
                apply(json.loads(app_json))
        # undo / run_start / run_end → no-op

    # Drop SYSTEM
    msgs = [m for m in state if m.get("role") != "system"]

    # Apply same post-fold defenses as production load_messages so the strict
    # replay compares end-to-end output. Defenses are not part of fold algebra
    # but are mandatory consumer-facing normalization (DeepSeek "assistant must
    # have thinking", Anthropic "tool_use must be paired"). See
    # ``agent_run_action_service._is_reasoning_only_assistant`` and
    # ``_ensure_tool_use_paired`` for matching production logic.
    msgs = _drop_reasoning_only_assistants_dict(msgs)
    msgs = _ensure_tool_use_paired_dict(msgs)
    return msgs


def _drop_reasoning_only_assistants_dict(msgs: list[dict]) -> list[dict]:
    out = []
    for m in msgs:
        if m.get("role") != "assistant":
            out.append(m)
            continue
        content = m.get("content") or []
        if not isinstance(content, list) or not content:
            out.append(m)
            continue
        if all(isinstance(b, dict) and b.get("type") == "reasoning" for b in content):
            continue  # drop
        out.append(m)
    return out


def _ensure_tool_use_paired_dict(msgs: list[dict]) -> list[dict]:
    """Mirror ``_ensure_tool_use_paired`` for raw dict messages."""
    tool_use_at: dict[str, int] = {}
    for i, m in enumerate(msgs):
        if m.get("role") != "assistant":
            continue
        for b in m.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                tu_id = b.get("id")
                if tu_id is not None:
                    tool_use_at[str(tu_id)] = i
    seen_results: set[str] = set()
    for m in msgs:
        if m.get("role") != "tool":
            continue
        for b in m.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                tr_id = b.get("tool_use_id")
                if tr_id is not None:
                    seen_results.add(str(tr_id))
    orphans = [(tu, pos) for tu, pos in tool_use_at.items() if tu not in seen_results]
    if not orphans:
        return msgs
    orphans.sort(key=lambda x: -x[1])
    out = list(msgs)
    for tu_id, pos in orphans:
        synthetic = {
            "id": f"synth-tool-result-{tu_id}",  # deterministic — matches production
            "role": "tool",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "content": (
                        "Tool execution did not complete. Synthesized by NexAU at fold time to "
                        "maintain the tool_use ↔ tool_result pairing invariant required by "
                        "Anthropic / OpenAI APIs. The original tool failure is in the trace logs."
                    ),
                    "is_error": True,
                }
            ],
        }
        out.insert(pos + 1, synthetic)
    return out


# ============================================================================
# Cross-key leak classification
# ============================================================================


def classify_cross_key_reuse(
    sig_by_key: dict[SessionKey, list[StrictSignature]],
) -> tuple[
    list[tuple[str, list[SessionKey]]],
    list[tuple[str, list[SessionKey], set[tuple]]],
]:
    """Find message ids reused across keys, classify by content.

    Returns:
      (same_content_reuses, diff_content_reuses)

    same_content_reuses: id appears in multiple keys with IDENTICAL content
        signature. Most common cause: legitimate session fork / migration
        (e.g. one agent REPLACE-imports another agent's history).

    diff_content_reuses: id appears in multiple keys with DIFFERENT content.
        Almost always a real bug — dedup misclassification, accidental id
        reuse by the producer, or a writer race. Investigate every case.

    Each tuple in ``diff_content_reuses`` carries the set of distinct content
    signatures so callers can drill in.
    """
    id_to_keys: dict[str, set[SessionKey]] = defaultdict(set)
    id_to_content_sigs: dict[str, set[tuple]] = defaultdict(set)

    for key, sigs in sig_by_key.items():
        for sig in sigs:
            mid = sig[0]
            id_to_keys[mid].add(key)
            id_to_content_sigs[mid].add(sig[1:])  # everything except id

    same_content: list[tuple[str, list[SessionKey]]] = []
    diff_content: list[tuple[str, list[SessionKey], set[tuple]]] = []
    for mid, keys in id_to_keys.items():
        if len(keys) <= 1:
            continue
        content_sigs = id_to_content_sigs[mid]
        if len(content_sigs) == 1:
            same_content.append((mid, sorted(keys)))
        else:
            diff_content.append((mid, sorted(keys), content_sigs))
    return same_content, diff_content


# ============================================================================
# End-to-end replay
# ============================================================================


class ReplayResult:
    """Result of a strict replay run on one DB."""

    def __init__(self) -> None:
        self.total_sessions = 0
        self.matches = 0
        self.mismatches: list[str] = []  # privacy-safe diff descriptions
        self.errors: list[str] = []
        self.same_content_reuses = 0
        self.diff_content_reuses = 0  # **THE ONE THAT MATTERS** — real bugs
        self.diff_content_samples: list[tuple[str, list[SessionKey], set[tuple]]] = []
        self.timings_ms: list[float] = []

    @property
    def all_clean(self) -> bool:
        """True iff: 100% oracle parity, 0 errors, 0 different-content reuse."""
        return self.matches == self.total_sessions and not self.errors and self.diff_content_reuses == 0

    def summarize(self) -> str:
        ts = sorted(self.timings_ms)
        median = ts[len(ts) // 2] if ts else 0.0
        p95 = ts[int(len(ts) * 0.95)] if ts else 0.0
        max_ms = max(ts) if ts else 0.0
        lines = [
            f"Oracle parity:        {self.matches}/{self.total_sessions} ({100 * self.matches / max(self.total_sessions, 1):.1f}%)",
            f"Errors:               {len(self.errors)}",
            f"Cross-key id reuse:   {self.same_content_reuses + self.diff_content_reuses}",
            f"  same content (legit fork/migration):  {self.same_content_reuses}",
            f"  DIFFERENT content (REAL BUG):         {self.diff_content_reuses}",
            f"Fold timings (ms):    median={median:.2f} p95={p95:.2f} max={max_ms:.2f}",
        ]
        return "\n".join(lines)


async def replay_strict(db_path: str) -> ReplayResult:
    """Run strict replay on a sqlite DB containing ``agent_run_actions`` table.

    Validates Caveats 2/3/4 (oracle parity bypassing ORM+Pydantic) and
    Caveat 5 (cross-key isolation) in a single sweep.
    """
    result = ReplayResult()

    eng = SQLDatabaseEngine.from_url(f"sqlite+aiosqlite:///{db_path}")
    try:
        svc = AgentRunActionService(engine=eng)
        all_actions = await eng.find_many(AgentRunActionModel, filters=AndFilter(filters=[]))
        keys = sorted({(a.user_id, a.session_id, a.agent_id) for a in all_actions})
        result.total_sessions = len(keys)

        sig_by_key: dict[SessionKey, list[StrictSignature]] = {}

        for key in keys:
            ag_key = AgentRunActionKey(user_id=key[0], session_id=key[1], agent_id=key[2])
            try:
                t0 = time.perf_counter_ns()
                prod_msgs = await svc.load_messages(key=ag_key)
                result.timings_ms.append((time.perf_counter_ns() - t0) / 1e6)

                prod_sig = strict_signature(prod_msgs)
                oracle_dicts = oracle_fold(db_path, key)
                oracle_sig = strict_signature(oracle_dicts)

                if prod_sig == oracle_sig:
                    result.matches += 1
                else:
                    # Privacy-safe diff: positions and field names only
                    diff_msg = f"  MISMATCH session={key[1][:8]}: prod={len(prod_sig)} msgs, oracle={len(oracle_sig)} msgs"
                    for i, (p, o) in enumerate(zip(prod_sig, oracle_sig)):
                        if p != o:
                            diff_fields = [
                                f for f, pv, ov in zip(("id", "role", "text_chars", "block_count", "block_types"), p, o) if pv != ov
                            ]
                            diff_msg += f" first diff @ idx {i} in {diff_fields}"
                            break
                    result.mismatches.append(diff_msg)

                sig_by_key[key] = prod_sig
            except Exception as e:  # noqa: BLE001 — collect, don't crash
                result.errors.append(f"  ERROR session={key[1][:8]}: {type(e).__name__}: {str(e)[:100]}")

        # Cross-key reuse classification
        same, diff = classify_cross_key_reuse(sig_by_key)
        result.same_content_reuses = len(same)
        result.diff_content_reuses = len(diff)
        result.diff_content_samples = diff[:5]  # keep a few for diagnostic
    finally:
        await eng._engine.dispose()

    return result


def run_replay_sync(db_path: str) -> ReplayResult:
    """Sync wrapper for tests / CLI."""
    return asyncio.run(replay_strict(db_path))
