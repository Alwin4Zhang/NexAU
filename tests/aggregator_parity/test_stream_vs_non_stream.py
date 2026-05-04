# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Stream-vs-non-stream parity (RFC-0023 §阶段 ① — vendor truth axis).

Set A vs Set B parity is **necessary but not sufficient** for the RFC-0023
§阶段 ③ retirement of Set B. The two could agree with each other and still
both diverge from the vendor's canonical aggregation.

The risk that motivates this third axis is **prompt cache hit rate**: when
an aggregated assistant Message is replayed back to the vendor on the next
turn (history compaction, multi-turn tool loops, agent-of-agent flows),
its byte-shape must match what the vendor itself would have produced from
a non-stream call — otherwise the prompt cache prefix breaks and we
silently lose latency / cost without any test ever flagging it.

This file picks up any fixture pair on disk:

    fixtures/<provider>/recordings/<scenario>.sse
    fixtures/<provider>/recordings/<scenario>.non_stream.json

…runs the SSE through Set A's aggregator + the reconstructor, loads the
non-stream JSON, reduces both to a UMP ``Message``, and asserts **structural**
equivalence — block count / order / type / tool-name / tool-input-keys / id
format prefix.

Note: this is **not** byte-level equality. The two recordings come from two
independent LLM calls (one streaming, one not), and LLMs are non-deterministic
across calls. The cache-prefix risk we're verifying is about message **shape**,
not token content — see ``compare_structural`` in ``parity_helpers.py``.

How to add a fixture pair: see ``scripts/record_fixture.py --also-non-stream``.

Filename note
-------------
Avoids the substrings ``openai`` / ``chat`` / ``llm`` so conftest.py's
auto-marker doesn't gate this on a real LLM API key — these recordings
are static fixtures, not live calls.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.aggregator_parity.parity_helpers import (
    NON_STREAM_LOADERS,
    ParityReport,
    compare_structural,
)
from tests.aggregator_parity.reconstructor import reconstruct_message_from_agui
from tests.aggregator_parity.sse_loader import load_recording

_RECORDINGS_ROOT = Path(__file__).resolve().parent / "fixtures"

# Provider directory → ID prefix that does NOT collide with conftest's
# auto-marker substrings (matches the convention in test_meta_self.py).
_PROVIDER_ABBREV = {
    "anthropic": "ant",
    "openai_chat": "ocomp",
    "openai_responses": "oresp",
    "gemini_rest": "gem",
}


def _set_a_runner(provider: str):
    """Return the run_set_a callable for ``provider``.

    Imported lazily so a missing optional SDK on a partial install doesn't
    break collection of unrelated provider tests.
    """
    if provider == "anthropic":
        from tests.aggregator_parity.anthropic_glue import run_set_a_anthropic

        return run_set_a_anthropic
    if provider == "openai_chat":
        from tests.aggregator_parity.openai_chat_glue import run_set_a_openai_chat

        return run_set_a_openai_chat
    if provider == "openai_responses":
        from tests.aggregator_parity.openai_responses_glue import (
            run_set_a_openai_responses_events_only,
        )

        return run_set_a_openai_responses_events_only
    if provider == "gemini_rest":
        from tests.aggregator_parity.gemini_glue import run_set_a_gemini

        return run_set_a_gemini
    raise KeyError(provider)


def _all_pairs() -> list[tuple[str, str, str, Path]]:
    """Discover ``(test_id, provider, scenario, non_stream_path)`` for every
    fixture pair on disk where BOTH ``.sse`` and ``.non_stream.json`` exist."""
    pairs: list[tuple[str, str, str, Path]] = []
    for provider, abbrev in _PROVIDER_ABBREV.items():
        rec_dir = _RECORDINGS_ROOT / provider / "recordings"
        if not rec_dir.is_dir():
            continue
        for non_stream_path in sorted(rec_dir.glob("*.non_stream.json")):
            scenario = non_stream_path.name.removesuffix(".non_stream.json")
            sse_path = rec_dir / f"{scenario}.sse"
            if not sse_path.is_file():
                # Orphan non-stream file (no matching SSE) — flagged in
                # test_orphan_non_stream_files below.
                continue
            test_id = f"{abbrev}_{scenario}"
            pairs.append((test_id, provider, scenario, non_stream_path))
    return pairs


# Fixtures known to exhibit a real divergence between the streamed
# aggregation and the vendor's non-stream response. Each entry is a
# precise design discussion, not a bug — an entry here means we have
# verified Set A's reconstruction does NOT byte-match the vendor's own
# aggregation, and that closing it requires an upstream decision.
#
# Format: (provider, scenario) → reason string.
KNOWN_VENDOR_TRUTH_DIVERGENCES: dict[tuple[str, str], str] = {
    ("anthropic", "vt_server_tool_use"): (
        "Set A reconstructs claude-sonnet-4-5 web_search streams as 2 blocks "
        "(ToolUseBlock + 1 collapsed TextBlock); the vendor's own non-stream "
        "response keeps 8 blocks (ToolUseBlock + 7 separate TextBlocks, each "
        "paired with its own citations referring to a different "
        "web_search_result chunk). Same root cause as the matching Set A↔Set B "
        "divergence in test_anthropic_parity.KNOWN_DIVERGENT_FIXTURES — "
        "Set A's text deltas only carry message_id (not block-level ID), so "
        "the reconstructor collapses consecutive text. Closing this requires "
        "RFC-0023 §阶段 ② to decide: should server_tool_use + citations_delta "
        "be a first-class block in UMP? How should multiple text blocks tied "
        "to distinct citations be merged vs. preserved? Design discussion, "
        "not a single-line bug."
    ),
}


_PAIRS = _all_pairs()


@pytest.mark.skipif(
    not _PAIRS, reason="No <scenario>.non_stream.json fixtures on disk yet — record some with scripts/record_fixture.py --also-non-stream"
)
@pytest.mark.parametrize(
    "provider,scenario,non_stream_path",
    [(p, s, np) for _id, p, s, np in _PAIRS],
    ids=[test_id for test_id, _, _, _ in _PAIRS],
)
def test_set_a_matches_vendor_non_stream(
    provider: str,
    scenario: str,
    non_stream_path: Path,
    request,
) -> None:
    """Set A's reconstructed Message must strongly match the vendor's non-stream JSON."""
    if (provider, scenario) in KNOWN_VENDOR_TRUTH_DIVERGENCES:
        request.applymarker(
            pytest.mark.xfail(
                reason=KNOWN_VENDOR_TRUTH_DIVERGENCES[(provider, scenario)],
                strict=True,
            )
        )

    # Set A path: SSE → AG-UI events → Message
    sse_events = load_recording(provider, scenario)
    run_set_a = _set_a_runner(provider)
    agui_events = run_set_a(sse_events)
    msg_a = reconstruct_message_from_agui(agui_events)

    # Vendor truth path: non-stream JSON → Message
    raw = non_stream_path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    msg_vendor = NON_STREAM_LOADERS[provider](payload)

    # Sanity guard against compare_structural's "two empty Messages match
    # trivially" corner: every recorded prompt is designed to elicit content,
    # so a non-stream payload that reduces to zero blocks is itself a
    # regression (likely vendor returned an error response, or the loader
    # silently dropped everything). Catch it here so we don't quietly pass.
    assert len(msg_vendor.content) > 0, (
        f"vendor non-stream payload for {provider}/{scenario} reduced to 0 blocks. "
        "Either the recording is an error response that snuck past validation, "
        "or NON_STREAM_LOADERS dropped its content silently. Inspect "
        f"{non_stream_path.name}."
    )

    # Route through ParityReport so the vendor-truth axis populates the same
    # report object the other axes use — keeps the data path uniform for any
    # future gap_report.md generator.
    report = ParityReport(
        fixture=f"{provider}/{scenario}",
        vendor_truth_failures=compare_structural(msg_a, msg_vendor),
    )
    if not report.vendor_truth_ok:
        pytest.fail(
            f"Vendor-truth parity failure:\n{report}\n\n"
            "This means Set A's stream aggregator produces a Message that does NOT\n"
            "match the vendor's own non-stream aggregation. Replaying this Message\n"
            "back to the vendor will MISS the prompt cache prefix and silently\n"
            "regress latency / cost on multi-turn flows.\n\n"
            "Either fix Set A to match the vendor, or — if the divergence is a\n"
            "deliberate design call — add an entry to KNOWN_VENDOR_TRUTH_DIVERGENCES\n"
            "in this file with a written rationale.",
        )


def test_orphan_non_stream_files() -> None:
    """Every ``<scenario>.non_stream.json`` must have a matching ``<scenario>.sse``.

    Catches accidentally-committed orphan files that would otherwise silently
    skip the parity assertion above (parametrize ignores them).
    """
    orphans: list[Path] = []
    for provider in _PROVIDER_ABBREV:
        rec_dir = _RECORDINGS_ROOT / provider / "recordings"
        if not rec_dir.is_dir():
            continue
        for non_stream_path in rec_dir.glob("*.non_stream.json"):
            scenario = non_stream_path.name.removesuffix(".non_stream.json")
            if not (rec_dir / f"{scenario}.sse").is_file():
                orphans.append(non_stream_path)
    assert not orphans, f"Found orphan non_stream.json files (no matching .sse): {[str(o) for o in orphans]}"
