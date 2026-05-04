"""Gemini REST fixtures for parity testing.

All fixtures are LIVE RECORDINGS from generativelanguage.googleapis.com
(via the streamGenerateContent endpoint with alt=sse). Both Set A and
Set B Gemini aggregators consume plain dicts, so no SDK normalization
is needed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

FixtureFn = Callable[[], list[dict[str, Any]]]


def _make_recording_fixture(scenario: str) -> FixtureFn:
    from tests.aggregator_parity.sse_loader import load_recording

    def loader() -> list[dict[str, Any]]:
        return load_recording("gemini_rest", scenario)

    loader.__name__ = f"fixture_recording_{scenario}"
    return loader


GEMINI_REST_FIXTURES: list[tuple[str, FixtureFn]] = [
    # gemini-2.5-flash recordings
    ("rec_plain_text", _make_recording_fixture("plain_text")),
    ("rec_thinking_then_text", _make_recording_fixture("thinking_then_text")),
    ("rec_thinking_then_tool", _make_recording_fixture("thinking_then_tool")),
    ("rec_tool_call", _make_recording_fixture("tool_call")),
    ("rec_long_text", _make_recording_fixture("long_text")),
    ("rec_vision", _make_recording_fixture("vision")),
    ("rec_system_instruction", _make_recording_fixture("system_instruction")),
    # gemini-flash-lite-latest variant
    ("rec_flash_lite_plain", _make_recording_fixture("flash_lite_plain")),
    # Vendor-truth pair sources — also run on axis 1 / 2.
    ("rec_vt_plain", _make_recording_fixture("vt_plain")),
    ("rec_vt_tool", _make_recording_fixture("vt_tool")),
    ("rec_vt_thinking", _make_recording_fixture("vt_thinking")),
]
