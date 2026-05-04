# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Parity tests for the Gemini REST aggregators.

RFC-0023 §阶段 ①.

File named ``test_gemini_aggregator_parity.py`` (not
``test_gemini_parity.py``) for symmetry with the Chat / Responses parity
test naming. Gemini doesn't trigger the ``llm`` / ``openai`` / ``chat``
auto-marker keywords, so no special workaround is needed for skip behavior.
"""

from __future__ import annotations

import pytest

from tests.aggregator_parity.fixtures.gemini_rest import GEMINI_REST_FIXTURES
from tests.aggregator_parity.gemini_glue import (
    run_set_a_gemini,
    run_set_b_gemini,
)
from tests.aggregator_parity.parity_helpers import (
    gemini_set_b_dict_to_message,
    run_parity,
)

KNOWN_DIVERGENT_FIXTURES: dict[str, str] = {
    "rec_vt_tool": (
        "Real-world divergence captured from gemini-3-flash-preview + "
        "functionCall. The wire stream's final chunk emits an empty text part "
        '(``{"text": ""}``) alongside ``finishReason=STOP`` right after the '
        "functionCall part. Set A's GeminiRestEventAggregator skips the "
        "zero-length text → 1 block (ToolUseBlock only). Set B's "
        "GeminiRestStreamAggregator preserves the empty text → 2 blocks "
        "(TextBlock(text='') + ToolUseBlock). Both behaviors are defensible: "
        "Set A is more semantically correct (an empty text block carries no "
        "signal), Set B is wire-faithful. RFC-0023 §阶段 ② to decide the "
        "canonical rule — silently dropping the empty terminator is the "
        "likely choice but it must be deliberate."
    ),
}


@pytest.mark.parametrize(
    "fixture_name,fixture_fn",
    GEMINI_REST_FIXTURES,
    ids=[name for name, _ in GEMINI_REST_FIXTURES],
)
def test_gemini_aggregator_parity(fixture_name: str, fixture_fn, request) -> None:
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
        run_set_a=run_set_a_gemini,
        run_set_b=run_set_b_gemini,
        set_b_to_message=gemini_set_b_dict_to_message,
    )

    if not report.strong_ok:
        pytest.fail(
            f"Strong parity failure on {fixture_name}:\n{report}\n"
            f"This indicates Set A and Set B drift on the same provider stream — "
            f"a real bug that must be fixed before RFC-0023 §阶段 ③ retires Set B."
        )


@pytest.mark.parametrize(
    "fixture_name,fixture_fn",
    GEMINI_REST_FIXTURES,
    ids=[name for name, _ in GEMINI_REST_FIXTURES],
)
def test_gemini_aggregator_weak_gaps_documented(
    fixture_name: str,
    fixture_fn,
    record_property,
) -> None:
    """Record (don't assert) the weak gaps. RFC-0023 §阶段 ② will close them."""
    events = fixture_fn()

    report = run_parity(
        fixture_name=fixture_name,
        events=events,
        run_set_a=run_set_a_gemini,
        run_set_b=run_set_b_gemini,
        set_b_to_message=gemini_set_b_dict_to_message,
    )

    record_property("strong_ok", report.strong_ok)
    record_property("gap_count", len(report.weak_gaps))
    for i, gap in enumerate(report.weak_gaps):
        record_property(f"gap_{i}_field", gap.field)
        record_property(f"gap_{i}_note", gap.note)
