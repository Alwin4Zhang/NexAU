# Sensitive Word Middleware

`SensitiveWordMiddleware` scans configured words in model input, tool results, and model
output. The on-hit action is configurable per side via `input_action` / `output_action`,
with three modes:

- `mask` (default): replace the hit text with `mask_template` (default `***`, the universal
  password-redaction convention); the run continues. Good for most chat scenarios — a single
  sensitive word does not abort the whole turn. Opt into category labels by passing
  `mask_template="[<{category}>]"`.
- `terminate`: stop the run with `force_stop_reason=ERROR_OCCURRED` and a refusal message;
  the run terminates as an error. The original RFC-0027 behavior, suitable for strict
  compliance contexts.
- `soft_reject`: replace/append the assistant message with a soft refusal and set
  `force_stop_reason=SUCCESS`. The run completes normally (not an error); callers see a
  graceful refusal as the final reply.

Every hit also emits a `ContentBlockedEvent` regardless of action, so audit pipelines
see the same signal.

The middleware does not ship with a default lexicon. You must configure at least one
lexicon source explicitly:

- `lexicon_dir`: directory of `.txt` files; each file name becomes the category.
- `lexicon_file`: a single `.txt` file; the file name becomes the category.
- `lexicon_words`: an inline iterable of words; words use the `explicit` category.

## YAML Configuration

```yaml
middlewares:
  - import: nexau.archs.main_sub.execution.middleware.sensitive_word:SensitiveWordMiddleware
    params:
      lexicon_dir: /opt/nexau/sensitive_lexicon
      case_sensitive: false
      block_input: true
      block_output: true
      # Default action is "mask"; choose "terminate" for the original RFC-0027 behavior
      # or "soft_reject" for a graceful refusal that still completes the run as SUCCESS.
      input_action: mask
      output_action: mask
      mask_template: "***"   # default; supports {category} / {word} / {length} placeholders
      raise_on_block: false
```

Each lexicon file should contain one word per line. Empty lines and lines starting with
`#` are ignored.

```text
# /opt/nexau/sensitive_lexicon/security.txt
internal-code-name
restricted phrase
```

## Python Configuration

```python
from nexau import AgentConfig
from nexau.archs.main_sub.execution.middleware.sensitive_word import SensitiveWordMiddleware

config = AgentConfig(
    name="safe_agent",
    middlewares=[
        SensitiveWordMiddleware(
            lexicon_dir="/opt/nexau/sensitive_lexicon",
            case_sensitive=False,
            block_input=True,
            block_output=True,
        )
    ],
)
```

## Behavior

- Input scanning runs in `before_model` for user, system, framework, and tool-result messages.
- Output scanning runs in `after_model` for the complete model response.
- Tool results are scanned before the next model call, after the tool result has been added
  to conversation history.
- On hit, the middleware applies `input_action` / `output_action`:
  - `mask` (default): replace the hit substrings in place; `force_stop_reason` is not set.
  - `terminate`: append/replace a refusal assistant message and set
    `force_stop_reason=ERROR_OCCURRED`.
  - `soft_reject`: same shape as `terminate` but with a softer template and
    `force_stop_reason=SUCCESS`.
- `raise_on_block=true` bypasses the action machinery and raises
  `SensitiveContentBlockedError` instead.

For a runnable example and a tiny sample lexicon, see `examples/sensitive_word/`.
