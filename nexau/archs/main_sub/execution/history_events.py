# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""HistoryEvent discriminated union — RFC-0026 forward-compat write channel.

Single typed slot on ``HookResult.history_event`` lets middleware emit any
history-affecting intent (REPLACE today; APPEND / UNDO / future) without
needing to add a new top-level field per event type. Adding a new event
type means adding one variant to the union — old SDK readers fall through
to ``UnknownEvent`` instead of breaking.

Each variant follows the protobuf-philosophy schema (RFC-0022):
``model_config = ConfigDict(extra='allow')`` + all secondary fields
optional, so a new SDK can add fields without breaking old SDK readers.

## Relationship to ``AgentRunActionModel`` (RFC-0022)

These event classes are the **middleware-facing intent envelope**;
``AgentRunActionModel`` (in ``nexau/archs/session/models/agent_run_action_model.py``)
is the **persisted DB row**. They sit at different layers:

  - Event = "I want to emit a REPLACE with these messages and this WHY"
    — no ownership keys, no action_id, no DB columns
  - Action = "row inserted into nexau_agent_run_actions" — has
    user_id / session_id / agent_id / run_id / root_run_id / action_id
    / created_at_ns + the JSONB columns

The typed extra payload (``ReplaceVariantBase`` / ``AppendExtra`` /
``UndoExtra``) is defined ONCE in ``agent_run_action_model.py`` and
shared by both layers — these event classes import them, never
re-declare. The discriminator value (``"replace"`` / ``"append"`` /
``"undo"``) is tied to ``RunActionType.*`` via ``Literal[RunActionType.X]``
so refactoring the enum string keeps both layers consistent.

Flow: middleware emits ``HistoryEvent`` → executor reads
``HookResult.history_event`` → routes to ``ctx.history.replace(...)``
→ ``HistoryList.replace_all`` → ``AgentRunActionService.persist_replace``
→ ``AgentRunActionModel.create_replace`` → DB INSERT.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Discriminator, Tag

from nexau.archs.session.models.agent_run_action_model import (
    AppendExtra,
    ReplaceVariantBase,
    RunActionType,
    UndoExtra,
)
from nexau.core.messages import Message

PROTOBUF_PHILOSOPHY = ConfigDict(extra="allow")


class ReplaceEvent(BaseModel):
    """Typed REPLACE — compaction / ``/clear`` / ``/compact <focus>``.

    Carries the new full message state + a typed reason variant
    discriminating WHY (``CompactAutoVariant`` / ``UserClearVariant`` /
    ``CompactFocusedVariant`` / ...). Reader-side replay can render or
    aggregate by reason without inspecting message content.

    Today's only producer is ``ContextCompactionMiddleware`` (regular +
    emergency paths). Future producers: ``/clear`` / ``/compact``
    handlers.
    """

    model_config = PROTOBUF_PHILOSOPHY
    # Tied to RunActionType so refactoring the enum string can't drift
    # the discriminator out of sync with the action layer.
    type: Literal[RunActionType.REPLACE] = RunActionType.REPLACE
    messages: list[Message]
    extra: ReplaceVariantBase


class AppendEvent(BaseModel):
    """Typed APPEND — placeholder for future iter-aware writers.

    Today APPENDs flow through ``HistoryList.append/extend`` (the list-
    interface side) and the executor's normal flush path; no middleware
    has needed to emit a typed APPEND through this channel. Reserved
    for future writers that want to carry ``AppendExtra`` (currently just
    ``trace_id``) without touching the list interface — e.g. tool-call
    boundary middleware that wants to tag APPENDs with run metadata.
    """

    model_config = PROTOBUF_PHILOSOPHY
    type: Literal[RunActionType.APPEND] = RunActionType.APPEND
    messages: list[Message]
    extra: AppendExtra | None = None


class UndoEvent(BaseModel):
    """Typed UNDO — placeholder for future ``/undo`` handler.

    No producer today. Reserved so when ``/undo`` ships it doesn't need
    a new ``HookResult`` field — just adds a variant to ``HistoryEvent``.
    """

    model_config = PROTOBUF_PHILOSOPHY
    type: Literal[RunActionType.UNDO] = RunActionType.UNDO
    before_run_id: str
    extra: UndoExtra | None = None


class UnknownEvent(BaseModel):
    """Forward-compat fallback for unknown discriminator values.

    Pydantic's typed discriminated union raises ``ValidationError`` on
    unknown ``type`` values. Routing unknowns through this catch-all
    variant via the callable ``Discriminator`` below means a future SDK
    that emits ``HistoryEvent(type="checkpoint", ...)`` decodes cleanly
    in an older SDK as ``UnknownEvent(type="checkpoint", ...)`` — the
    executor sees an event it doesn't understand and skips it (graceful
    no-op) instead of crashing the parser.

    Mirrors the ``UnknownReplaceVariant`` pattern in RFC-0022's
    ``ReplaceExtra`` discriminated union.
    """

    model_config = PROTOBUF_PHILOSOPHY
    type: str  # whatever the unknown discriminator value was


_KNOWN_EVENT_TYPES: frozenset[str] = frozenset({RunActionType.REPLACE.value, RunActionType.APPEND.value, RunActionType.UNDO.value})


def _discriminate_history_event(value: object) -> str:
    """Discriminator that buckets unknown ``type`` values to UnknownEvent.

    Without this callable form, a Pydantic discriminated union would
    raise on any ``type`` outside the declared literal set — locking
    the protocol against forward additions. The callable lets us route
    "anything else" to ``UnknownEvent``, preserving forward-compat.
    """
    # Pydantic feeds dicts (parsing from raw input) or model instances
    # (parsing from already-constructed objects). Both flows route to a
    # ``type`` field; cast to str defensively because dict values are
    # untyped and a malicious payload could put e.g. an int there.
    raw: object
    if isinstance(value, dict):
        raw = cast(dict[str, Any], value).get("type", "unknown")
    else:
        raw = getattr(value, "type", "unknown")
    tag = str(raw)
    return tag if tag in _KNOWN_EVENT_TYPES else "unknown"


HistoryEvent = Annotated[
    Annotated[ReplaceEvent, Tag(RunActionType.REPLACE.value)]
    | Annotated[AppendEvent, Tag(RunActionType.APPEND.value)]
    | Annotated[UndoEvent, Tag(RunActionType.UNDO.value)]
    | Annotated[UnknownEvent, Tag("unknown")],
    Discriminator(_discriminate_history_event),
]
"""Discriminated union covering all typed history-affecting events
emitted via ``HookResult.history_event``. Add new event types by
declaring a new ``BaseModel`` with a unique ``type: Literal[...]`` and
appending it to this union — old SDK readers fall through to
``UnknownEvent`` instead of crashing."""
