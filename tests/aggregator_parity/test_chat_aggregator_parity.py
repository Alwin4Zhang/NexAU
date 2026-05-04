# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Parity tests for the OpenAI Chat Completions aggregators.

RFC-0023 §阶段 ①.

Note: file is named ``test_chat_aggregator_parity.py`` (not
``test_openai_chat_parity.py``) because the conftest's
``pytest_collection_modifyitems`` hook auto-applies ``@pytest.mark.llm``
to any test whose name contains ``openai`` / ``chat`` / ``llm`` and the
autouse ``mock_env_vars`` fixture then skips them when no real API key
is in the environment. These pure unit tests don't need a live key —
they consume committed recordings — so we sidestep the auto-marker.
"""

from __future__ import annotations

import pytest

from tests.aggregator_parity.fixtures.openai_chat import OPENAI_CHAT_FIXTURES
from tests.aggregator_parity.openai_chat_glue import (
    run_set_a_openai_chat,
    run_set_b_openai_chat,
)
from tests.aggregator_parity.parity_helpers import (
    openai_chat_set_b_dict_to_message,
    run_parity,
)

# Fixtures known to expose real divergences.
KNOWN_DIVERGENT_FIXTURES: dict[str, str] = {}
# Previously contained 3 OpenRouter list-content fixtures lifted from
# test_llm_streaming.py. Removed after live recording confirmed the
# current OpenRouter API does NOT emit list-shape delta.content — see
# fixtures/openai_chat/__init__.py docstring for the full story.


@pytest.mark.parametrize(
    "fixture_name,fixture_fn",
    OPENAI_CHAT_FIXTURES,
    ids=[name for name, _ in OPENAI_CHAT_FIXTURES],
)
def test_completions_aggregator_parity(fixture_name: str, fixture_fn, request) -> None:
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
        run_set_a=run_set_a_openai_chat,
        run_set_b=run_set_b_openai_chat,
        set_b_to_message=openai_chat_set_b_dict_to_message,
    )

    if not report.strong_ok:
        pytest.fail(
            f"Strong parity failure on {fixture_name}:\n{report}\n"
            f"This indicates Set A and Set B drift on the same provider stream — "
            f"a real bug that must be fixed before RFC-0023 §阶段 ③ retires Set B."
        )


@pytest.mark.parametrize(
    "fixture_name,fixture_fn",
    OPENAI_CHAT_FIXTURES,
    ids=[name for name, _ in OPENAI_CHAT_FIXTURES],
)
def test_completions_aggregator_weak_gaps_documented(
    fixture_name: str,
    fixture_fn,
    record_property,
    request,
) -> None:
    """Record (don't assert) the weak gaps. RFC-0023 §阶段 ② will close them."""
    if fixture_name in KNOWN_DIVERGENT_FIXTURES:
        request.applymarker(pytest.mark.xfail(reason=KNOWN_DIVERGENT_FIXTURES[fixture_name], strict=True))

    events = fixture_fn()

    report = run_parity(
        fixture_name=fixture_name,
        events=events,
        run_set_a=run_set_a_openai_chat,
        run_set_b=run_set_b_openai_chat,
        set_b_to_message=openai_chat_set_b_dict_to_message,
    )

    record_property("strong_ok", report.strong_ok)
    record_property("gap_count", len(report.weak_gaps))
    for i, gap in enumerate(report.weak_gaps):
        record_property(f"gap_{i}_field", gap.field)
        record_property(f"gap_{i}_note", gap.note)
