# 2026-05-14 — Pyright `reportPrivateUsage` recurring trap

**TL;DR**: Twice within a single PR (#554) I named a cross-module helper
with a leading underscore (`_extract_status_code` reuse attempt, then
later `_record_thinking_signature_event`), shipped it through local
ruff/mypy that don't flag this, and only hit pyright's
`reportPrivateUsage` rule in PR CI. Both times CI failed identically on
typecheck + windows-quality. The fix each time was the same: rename to
the public form. Worth a case study because (a) it's a recurring class
of failure I clearly didn't internalize the first time, and (b)
pyright's underscore-name rule is a real "the linter is opinionated
about API hygiene" gotcha that diverges from PEP 8 convention.

**Date**: 2026-05-14
**Driver**: PR #554 (`fix/anthropic-orphan-thinking-signature`)
**Files**: `nexau/archs/main_sub/execution/llm_caller.py`,
`nexau/archs/main_sub/execution/model_response.py`,
`nexau/archs/main_sub/execution/middleware/llm_failover.py`
**Fix commits**: `3e99dab9` (1st occurrence), `3d91ef8b` (2nd)

---

## What pyright is enforcing

Pyright's `reportPrivateUsage` rule (enabled in nexau by default):

> A name that starts with a single underscore is "private to the module
> it's declared in". Importing it from another module → error.

This is **stricter than PEP 8's convention** (which only says "single
underscore = informal hint, not enforced"). Most Python tooling
(`ruff`, `mypy` in default config) treats it as advisory. Pyright
treats it as a hard error.

## Both occurrences

### 1st time (commit `3e99dab9` reverted from `d82c522b`):

Tried to reuse `llm_failover._extract_status_code` from `llm_caller.py`
to save 3 lines of duplicate OpenAI/Anthropic SDK status-code
extraction:

```python
# llm_caller.py
from nexau.archs.main_sub.execution.middleware.llm_failover import _extract_status_code

def _is_thinking_signature_error(exc: BaseException) -> bool:
    if not isinstance(exc, Exception) or _extract_status_code(exc) != 400:
        return False
    ...
```

Local: clean. CI: `_extract_status_code is private and used outside of
the module in which it is declared (reportPrivateUsage)` on typecheck
+ windows-quality.

Fix: inlined back to `isinstance(exc, anthropic.APIStatusError) and
exc.status_code == 400`. 3-line "save" wasn't worth a cross-module
private dep anyway.

### 2nd time (commit `3d91ef8b` fixing `2f574873`):

When adding Tier-1+2 observability for the orphan-thinking-signature
defense, I put a helper `_record_thinking_signature_event` in
`llm_caller.py` and imported it from `model_response.py`:

```python
# llm_caller.py
def _record_thinking_signature_event(layer: str, **fields: Any) -> None:
    ...

# model_response.py
from nexau.archs.main_sub.execution.llm_caller import _record_thinking_signature_event
```

Same error from pyright. Fix: renamed to
`record_thinking_signature_event` (drop the underscore — the helper
genuinely is module-public now, because `model_response.py` imports it).

---

## Why this kept happening

Two reinforcing factors:

1. **My local env doesn't run pyright by default.** I run ruff +
   `uv run pytest` and `uv run mypy`. Pyright lives in `make typecheck`
   target but I don't invoke it per-edit. Result: feedback loop only on
   CI, ~7 min per round-trip.

2. **I default to "underscore = private" PEP 8 convention.** Pyright's
   enforcement of the same convention is stricter than I'm used to —
   most codebases let internal helpers be underscored even when other
   modules need them, and just live with the soft warning.

## Detection heuristic for future Claude / agents

Before importing or defining a helper that's used cross-module:

1. If the helper is used from another module, **don't underscore it**.
   PEP 8 is fine with public names for module-internal helpers when
   another module legitimately needs them. Pyright is not fine with
   underscored names crossing boundaries.

2. Local quick-check before push:
   ```bash
   uv run pyright path/to/changed_file.py
   ```
   If this is clean, the `reportPrivateUsage` class of failures won't
   bite. Faster than `make typecheck` (which also runs mypy).

3. Even cleaner: alias `make typecheck-quick` to just run pyright on
   changed files, and run it before every push. (Not done in this PR;
   noted as follow-up.)

## What the right naming convention actually is

For nexau, after these two trips:

- **Helper used only within the same module**: underscore prefix OK.
- **Helper used across modules in the same package**: NO underscore.
  PEP 8's "single underscore = soft hint" gets overridden by pyright's
  enforcement here.
- **API exposed outside the package**: NO underscore + explicit
  `__all__` entry (separate concern).

The 2nd-time fix took the right form:
`record_thinking_signature_event` (no underscore) — both
`model_response.py` and `llm_caller.py` use it as a module-public
"observability hook helper" that happens to live in `llm_caller.py`
for proximity to the L4 logic.

## Related case study

[2026-05-14-live-test-env-var-convention.md](2026-05-14-live-test-env-var-convention.md)
documents the same shape of class-of-failure (NEXAU_LIVE_* vs
LIVE_ANTHROPIC_* naming drift). Pattern: **introducing a new convention
that doesn't match the established one, then having tooling silently
reject it**. The remediation rule is the same: grep for existing
conventions first; let the existing pattern teach you the constraint
before inventing a new name.
