# Aggregator Parity Gap Report

_Generated: 2026-05-03 08:33:27 UTC by `tests/aggregator_parity/scripts/gen_gap_report.py`._

This report is the input list for RFC-0023 §阶段 ②. It enumerates
every field Set A's event stream doesn't carry today (vs. Set B's
`finalize()` output) and every structural divergence between Set A's
aggregation and the vendor's own non-stream JSON response.

## Axis 1 — Set A vs Set B strong equivalence

| Provider | Pass | Known xfail | Total |
| --- | --- | --- | --- |
| `anthropic` | 34 | 2 | 36 |
| `gemini_rest` | 10 | 1 | 11 |
| `openai_chat` | 16 | 0 | 16 |
| `openai_responses` | 16 | 0 | 16 |

**Registered known divergences (strict xfail in `KNOWN_DIVERGENT_FIXTURES` — design discussions for §阶段 ②):**

| Provider | Fixture | Failure |
| --- | --- | --- |
| `anthropic` | `rec_server_tool_use` | block count mismatch: a=2 b=6 (a_types=['ToolUseBlock', 'TextBlock'], b_types=['ToolUseBlock', 'TextBlock', 'TextBlock', 'TextBlock', 'TextBlock', 'TextBlock']) |
| `anthropic` | `rec_vt_server_tool_use` | block count mismatch: a=2 b=6 (a_types=['ToolUseBlock', 'TextBlock'], b_types=['ToolUseBlock', 'TextBlock', 'TextBlock', 'TextBlock', 'TextBlock', 'TextBlock']) |
| `gemini_rest` | `rec_vt_tool` | block count mismatch: a=1 b=2 (a_types=['ToolUseBlock'], b_types=['TextBlock', 'ToolUseBlock']) |

## Axis 2 — Set A weak gaps (target list for §阶段 ②)

These fields are present on Set B's `finalize()` dict but cannot
be reconstructed from the AG-UI event stream into a UMP `Message`.
Two paths to close them in §阶段 ②: (a) add the fields to `Message`,
or (b) have the gap-checker consume `LLMCallMetadataEvent` from
the agui event stream directly. Either way, the gap is at the
`Message`-shape level even though Set A already emits the metadata.

| Field | Fixtures | Sample provider | Sample fixture | Note |
| --- | --- | --- | --- | --- |
| `top_level.model` | 66 | `anthropic` | `plain_text` | no Set A event carries this — target for LLMCallMetadataEvent |
| `top_level.usage` | 61 | `anthropic` | `plain_text` | carried indirectly by UsageUpdateEvent emitted from middleware reading Set B today |
| `top_level.stop_reason` | 27 | `anthropic` | `plain_text` | no Set A event carries this — target for LLMCallMetadataEvent |
| `block[0].ReasoningBlock.signature` | 4 | `anthropic` | `rec_claude_thinking_real` | ThinkingTextMessage* event extension target |

## Axis 3 — Set A vs vendor non-stream JSON

- Total pairs: **13**
- Structural match: **12**
- Known divergences (registered xfail): **1**
- Unregistered failures: **0**

**Known divergences (design discussions for §阶段 ②):**

| Provider | Scenario | Failure |
| --- | --- | --- |
| `anthropic` | `vt_server_tool_use` | block count mismatch: a=2 b=8 (a_types=['ToolUseBlock', 'TextBlock'], b_types=['ToolUseBlock', 'TextBlock', 'TextBlock', 'TextBlock', 'TextBlock', 'TextBlock', 'TextBlock', 'TextBlock']) |

## Coverage

| Provider | Fixtures (axis 1) | Vendor-truth pairs (axis 3) |
| --- | --- | --- |
| `anthropic` | 36 | 4 |
| `gemini_rest` | 11 | 3 |
| `openai_chat` | 16 | 3 |
| `openai_responses` | 16 | 3 |

---

Regenerate: `uv run tests/aggregator_parity/scripts/gen_gap_report.py`
