# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Parity tests for the Anthropic aggregators.

RFC-0023 §阶段 ①.

For each fixture, drive the same synthetic provider event stream through both
Set A (``llm_aggregators.AnthropicEventAggregator``) and Set B
(``llm_caller.AnthropicStreamAggregator``), reduce both outputs to a UMP
``Message``, and compare:

- **Strong** equivalence: role + content blocks (count/order/type/main fields).
  Any failure here = real drift between the two implementations and must be
  fixed before RFC-0023 §阶段 ③ retires Set B.

- **Weak** gaps: fields only present in Set B's output today (usage,
  stop_reason, model_name, reasoning signature/redacted_data). Recorded but
  do not fail the test — they are the precise targets for RFC-0023 §阶段 ②.
"""

from __future__ import annotations

import pytest

from tests.aggregator_parity.anthropic_glue import (
    run_set_a_anthropic,
    run_set_b_anthropic,
)
from tests.aggregator_parity.fixtures.anthropic import ANTHROPIC_FIXTURES
from tests.aggregator_parity.parity_helpers import (
    anthropic_set_b_dict_to_message,
    run_parity,
)

# Fixtures known to expose real divergences between Set A and Set B.
# Tracked for follow-up resolution before RFC-0023 §阶段 ③ (Set B retirement).
#
# Resolved divergences (kept here for historical reference; the fixtures
# now pass under the production aggregators):
#
# - real_thinking_delta_without_block_start: previously Set A dropped orphan
#   thinking_delta entirely. Fixed by mirroring Set B's lazy-block-synthesis
#   pattern in AnthropicEventAggregator._handle_content_block_delta — when a
#   thinking_delta arrives without a preceding content_block_start, we now
#   synthesize a thinking_id + emit ThinkingTextMessageStartEvent on the fly,
#   same as what _flush_pending_with_synthetic does for tool_use blocks.
KNOWN_DIVERGENT_FIXTURES: dict[str, str] = {
    "rec_vt_server_tool_use": (
        "Same root cause as ``rec_server_tool_use`` below — claude-sonnet-4-5 "
        "+ web_search server tool. Set A's reconstructor collapses 7 separate "
        "TextBlocks (each tied to a distinct citations group) into 1; Set B "
        "preserves them. Recorded as the vendor-truth pair for axis 3 (also "
        "registered in test_stream_vs_non_stream.KNOWN_VENDOR_TRUTH_DIVERGENCES) "
        "but the same shape mismatch surfaces on axis 1 too. Closing requires "
        "RFC-0023 §阶段 ② design call on the UMP block model for "
        "server_tool_use + citations_delta."
    ),
    "rec_server_tool_use": (
        "Real-world divergence captured from claude-sonnet-4-5 + web_search "
        "server tool. Wire stream contains multiple content_block_start "
        "events with type=text interspersed with citations_delta events "
        "(citations referring to web_search_result content). Set B emits 5 "
        "separate TextBlocks (one per content_block_start). Set A's text "
        "handling collapses consecutive text into one TextBlock through "
        "the reconstructor's flush_text logic. Plus Set A doesn't model "
        "the web_search_tool_result block type at all (handled as 'unknown'); "
        "Set B preserves the search results in its output. Resolution requires "
        "§阶段 ② to decide: should server_tool_use + citations_delta be a "
        "first-class block in UMP? How should multiple text blocks be merged "
        "vs. preserved? This is a design discussion, not a single-line bug."
    ),
}


@pytest.mark.parametrize("fixture_name,fixture_fn", ANTHROPIC_FIXTURES, ids=[name for name, _ in ANTHROPIC_FIXTURES])
def test_anthropic_parity(fixture_name: str, fixture_fn, request) -> None:
    """Set A and Set B must produce strongly-equivalent Messages on the same input."""
    if fixture_name in KNOWN_DIVERGENT_FIXTURES:
        request.applymarker(
            pytest.mark.xfail(
                reason=KNOWN_DIVERGENT_FIXTURES[fixture_name],
                strict=True,
            )
        )

    events = fixture_fn()

    report = run_parity(
        fixture_name=fixture_name,
        events=events,
        run_set_a=run_set_a_anthropic,
        run_set_b=run_set_b_anthropic,
        set_b_to_message=anthropic_set_b_dict_to_message,
    )

    if not report.strong_ok:
        pytest.fail(
            f"Strong parity failure on {fixture_name}:\n{report}\n"
            f"This indicates Set A and Set B drift on the same provider stream — "
            f"a real bug that must be fixed before RFC-0023 §阶段 ③ retires Set B."
        )


@pytest.mark.parametrize("fixture_name,fixture_fn", ANTHROPIC_FIXTURES, ids=[name for name, _ in ANTHROPIC_FIXTURES])
def test_anthropic_weak_gaps_are_documented(
    fixture_name: str,
    fixture_fn,
    record_property,
) -> None:
    """Record (don't assert) the weak gaps. RFC-0023 §阶段 ② will close them.

    Once §阶段 ② lands, this test should be augmented with assertions that
    each gap is closed. For now it simply records gaps as JUnit XML test
    properties so CI / dashboards can track the gap surface area over time.
    """
    events = fixture_fn()

    report = run_parity(
        fixture_name=fixture_name,
        events=events,
        run_set_a=run_set_a_anthropic,
        run_set_b=run_set_b_anthropic,
        set_b_to_message=anthropic_set_b_dict_to_message,
    )

    record_property("strong_ok", report.strong_ok)
    record_property("gap_count", len(report.weak_gaps))
    for i, gap in enumerate(report.weak_gaps):
        record_property(f"gap_{i}_field", gap.field)
        record_property(f"gap_{i}_note", gap.note)
