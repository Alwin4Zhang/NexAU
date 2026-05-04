# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Parity tests for the OpenAI Responses aggregators.

RFC-0023 §阶段 ①.
"""

from __future__ import annotations

import pytest

from tests.aggregator_parity.fixtures.openai_responses import OPENAI_RESPONSES_FIXTURES
from tests.aggregator_parity.openai_responses_glue import (
    run_set_a_openai_responses_events_only,
    run_set_b_openai_responses,
)
from tests.aggregator_parity.parity_helpers import (
    openai_responses_set_b_dict_to_message,
    run_parity,
)

# Fixtures known to expose real divergences. Tracked for resolution before
# RFC-0023 §阶段 ③ retires Set B.
# Fixtures known to expose real divergences. Tracked for resolution before
# RFC-0023 §阶段 ③ retires Set B.
#
# Resolved divergences (kept here for historical reference):
#
# - rec_gpt5_tool_with_reasoning + rec_gpt5_high_reasoning: previously Set A
#   emitted no thinking events when gpt-5.x produced reasoning silently
#   (no summary_part.added → no ThinkingTextMessageStartEvent). Set B
#   persisted an empty ReasoningBlock as a 'reasoning happened' marker.
#   Fixed by emitting ThinkingTextMessage{Start,End} from
#   _ReasoningItemAggregator at the output_item.added boundary (and on
#   finish() invoked by output_item.done). Idempotent with the existing
#   summary_part.added Start emission so the normal flow is unchanged.
KNOWN_DIVERGENT_FIXTURES: dict[str, str] = {}


@pytest.mark.parametrize(
    "fixture_name,fixture_fn",
    OPENAI_RESPONSES_FIXTURES,
    ids=[name for name, _ in OPENAI_RESPONSES_FIXTURES],
)
def test_responses_aggregator_parity(fixture_name: str, fixture_fn, request) -> None:
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
        run_set_a=run_set_a_openai_responses_events_only,
        run_set_b=run_set_b_openai_responses,
        set_b_to_message=openai_responses_set_b_dict_to_message,
    )

    if not report.strong_ok:
        pytest.fail(
            f"Strong parity failure on {fixture_name}:\n{report}\n"
            f"This indicates Set A and Set B drift on the same provider stream — "
            f"a real bug that must be fixed before RFC-0023 §阶段 ③ retires Set B."
        )


@pytest.mark.parametrize(
    "fixture_name,fixture_fn",
    OPENAI_RESPONSES_FIXTURES,
    ids=[name for name, _ in OPENAI_RESPONSES_FIXTURES],
)
def test_responses_aggregator_weak_gaps_documented(
    fixture_name: str,
    fixture_fn,
    record_property,
) -> None:
    """Record (don't assert) the weak gaps. RFC-0023 §阶段 ② will close them."""
    events = fixture_fn()

    report = run_parity(
        fixture_name=fixture_name,
        events=events,
        run_set_a=run_set_a_openai_responses_events_only,
        run_set_b=run_set_b_openai_responses,
        set_b_to_message=openai_responses_set_b_dict_to_message,
    )

    record_property("strong_ok", report.strong_ok)
    record_property("gap_count", len(report.weak_gaps))
    for i, gap in enumerate(report.weak_gaps):
        record_property(f"gap_{i}_field", gap.field)
        record_property(f"gap_{i}_note", gap.note)
