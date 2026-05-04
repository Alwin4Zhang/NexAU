# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Parse recorded provider SSE byte streams into event dicts.

Supports two SSE flavors:
- Anthropic / OpenAI Responses style: ``event: <name>\\ndata: <json>\\n\\n``
- OpenAI Chat Completions style: ``data: <json>\\n\\n`` (no event line; type is
  inferred or implicit; ``data: [DONE]`` terminates)

Loaders return a list of dicts that can be:
- Fed directly to Set B's ``consume()`` (which is dict-permissive)
- Normalized to strict SDK types via the per-provider ``dict_to_*_event``
  helpers in the *_glue.py modules

Recordings live at ``tests/aggregator_parity/fixtures/<provider>/recordings/<scenario>.sse``
and are committed redacted (safety_identifier and prompt_cache_key replaced
with sentinel values; raw API keys never appear since they live in request
headers, not the response body).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_RECORDINGS_ROOT = Path(__file__).resolve().parent / "fixtures"


def _provider_recordings_dir(provider: str) -> Path:
    return _RECORDINGS_ROOT / provider / "recordings"


def list_recordings(provider: str) -> list[str]:
    """List ``.sse`` recording stem names for a provider."""
    d = _provider_recordings_dir(provider)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.sse"))


def _parse_sse_blocks(raw: str) -> list[dict[str, Any]]:
    """Parse SSE bytes into a list of dicts, one per data: line.

    Skips ``[DONE]`` sentinels (OpenAI Chat / Responses convention).
    """
    events: list[dict[str, Any]] = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        # An SSE block can have multiple lines: `event: foo\ndata: {...}`
        data_lines: list[str] = []
        for line in block.split("\n"):
            line = line.strip()
            if line.startswith("data:"):
                payload = line[len("data:") :].lstrip()
                if payload == "[DONE]":
                    continue
                data_lines.append(payload)
            # `event: <name>` lines are dropped — Anthropic SDK and OpenAI SDK
            # both rely on the JSON's own ``type`` field for dispatch, not on
            # the SSE event-name header. (We've verified this matches what
            # both SDKs do internally.)
        for payload in data_lines:
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Failed to parse SSE data block: {exc!r}\nPayload: {payload[:200]}") from exc
    return events


def load_recording(provider: str, scenario: str) -> list[dict[str, Any]]:
    """Load a recorded SSE fixture and return a list of event dicts."""
    path = _provider_recordings_dir(provider) / f"{scenario}.sse"
    if not path.is_file():
        raise FileNotFoundError(f"Recording not found: {path}")
    raw = path.read_text(encoding="utf-8")
    return _parse_sse_blocks(raw)
