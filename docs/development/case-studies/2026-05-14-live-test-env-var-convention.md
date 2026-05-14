# 2026-05-14 — Inventing parallel naming when the repo already had a convention

**TL;DR**: Wrote 8 new live integration tests in PR #554 and invented a
one-off env-var prefix `NEXAU_LIVE_BASE_URL` / `_API_KEY` / `_MODEL`. The
repo's existing convention is `LIVE_ANTHROPIC_*` (used by 4 other live
test files and already injected into PR CI). Result: all 8 tests
**silently skipped** in CI for two commit cycles, codecov/patch failed,
and I spent multiple rounds telling the user "CI doesn't have keys, we
need nightly workflow" — which was a fabricated explanation for a
symptom whose real cause was a name mismatch I introduced. Fixed by
renaming. The class of mistake is the same as inventing `drop_all_thinking`
earlier in the same PR when `allow_unsigned_thinking`'s default branch
already covered the pattern.

**Date**: 2026-05-14
**Driver**: PR #554 (`fix/anthropic-orphan-thinking-signature`)
**Files**:
- `tests/integration_live/test_anthropic_thinking_signature_live.py` (the
  test file I wrote)
- `.github/workflows/ci.yml:397-399` (where the real convention is
  injected for the test-saas job)
- `tests/integration/test_two_turn_payload_live.py`,
  `test_block_matrix_langfuse_live.py`,
  `test_aggregator_live_e2e.py` (existing examples I should have looked
  at first)
- Fix commit: `c20a3eab`

---

## Initial symptom

After pushing the live tests, PR CI showed:

- All 9 functional jobs green (lint / typecheck / test-saas / test-selfhost
  / windows-\* / etc.)
- `codecov/patch` red at ~54% (target 80%) — Layer 4's 4 retry paths
  (sync × async × stream × non-stream) all uncovered

My first read: "CI doesn't have an API key for the live tests, so they
skip — that's why codecov can't see Layer 4. We need to wire the nightly
workflow to upload coverage via carryforward."

That story matched the surface but **was wrong** at the root. I had not
read `.github/workflows/ci.yml`.

---

## Root cause (one layer down)

`test_anthropic_thinking_signature_live.py` was reading:

```python
BASE_URL = os.environ.get("NEXAU_LIVE_BASE_URL", "")
API_KEY = os.environ.get("NEXAU_LIVE_API_KEY", "")
MODEL = os.environ.get("NEXAU_LIVE_MODEL", "claude-opus-4-6")

pytestmark = pytest.mark.skipif(
    not (BASE_URL and API_KEY),
    reason="Live tests need NEXAU_LIVE_BASE_URL + NEXAU_LIVE_API_KEY",
)
```

PR CI's `test-saas` job was already injecting:

```yaml
LIVE_ANTHROPIC_API_KEY: ${{ secrets.NORTHGATE_API_KEY }}
LIVE_ANTHROPIC_BASE_URL: https://northgate.xiaobei.top
LIVE_ANTHROPIC_MODEL: claude-sonnet-4-5-20250929
NEXAU_RUN_LIVE_LLM_TESTS: "1"
```

Names don't match → my `skipif` evaluated to `True` → 8 tests skipped at
collection time → Layer 4 retry paths never executed → codecov saw
exactly the uncovered lines it reported.

Every other live test in the repo
(`test_two_turn_payload_live.py`,
`test_block_matrix_langfuse_live.py`,
`test_aggregator_live_e2e.py`) uses the `LIVE_ANTHROPIC_*` /
`LIVE_OPENAI_*` / `NORTHGATE_API_KEY` convention. I introduced
`NEXAU_LIVE_*` as a parallel naming with no precedent.

---

## How it stayed hidden

Three reinforcing factors:

1. **Local runs masked it.** I tested locally with
   `NEXAU_LIVE_BASE_URL=... NEXAU_LIVE_API_KEY=... uv run pytest`. 8/8
   passed against the real gateway. The names "worked" from my own
   shell.

2. **Plausible alternate explanation.** "CI doesn't have keys for live
   tests" is a legitimate pattern in many repos. I anchored on it as
   the diagnosis without checking `ci.yml`.

3. **I doubled down.** When codecov stayed red, I drafted a follow-up
   plan to wire the nightly workflow with `flags: unittests-nightly`
   carryforward. None of that would have helped — nightly *also* uses
   `LIVE_ANTHROPIC_*`, so the rename was needed either way.

The user broke the loop with one question: **"CI 里按理说是配了很多的
key 了吧"** ("CI should already have many keys configured, right?"). I
finally `grep`ed the workflow and the truth surfaced in 5 seconds.

---

## Lesson

**Before introducing any new name in an existing codebase, grep for the
existing convention first.** This applies to:

- Environment variables (`grep -r "LIVE_" .github/workflows/`)
- Pytest markers (`grep "live_nightly\|llm:" pytest.ini`)
- Config keys (`grep -r "<feature>_" nexau/`)
- File paths (`tests/integration_live/` vs the established
  `tests/integration/test_*_live.py` pattern — even this one was a
  divergence)
- Function parameters (`drop_all_thinking` invented when
  `allow_unsigned_thinking=False` default branch already covered the
  case)

The pattern is **always the same shape**: I write code that looks
"reasonable in isolation", the existing convention exists and my new
code can't talk to it, the test infra silently degrades to a no-op, and
my first explanation for the symptom is a fabrication that protects the
naming choice rather than questions it.

The remediation rule, stated affirmatively: **for any name you write
that has a peer somewhere in the codebase, the burden of proof is on
"why a new name", not on "why reuse"**.

---

## Detection heuristic for future Claude / agents

When a test you wrote skips in CI but runs locally, the first three
checks should be:

1. `grep -r "<your-env-var-name>" .github/workflows/ tests/`
   - If it appears only in your new file → naming mismatch, look for
     similar prefixes via `grep -rE "(LIVE|TEST|CI)_[A-Z_]+_API_KEY"`
2. Print env vars in the failing CI step:
   `printenv | grep -E "<related-prefix>" || echo "no match"`
3. Compare against another live test in the same dir:
   `head -20 tests/integration/test_*_live.py | grep getenv`

If any one of these had been my first move, the whole "we need nightly
carryforward" detour would not have happened.

---

## What changed

Single commit `c20a3eab`:

- Renamed `NEXAU_LIVE_BASE_URL` → `LIVE_ANTHROPIC_BASE_URL` (and
  `_API_KEY`, `_MODEL`)
- Updated the module-level docstring to point at the existing
  convention and explain why this file uses the same names
- No workflow change needed — `test-saas` job already exports the right
  names

After push, the 8 live tests run automatically in PR CI, exercising
Layer 4's 4 retry paths against a real gateway, and codecov/patch
recovers without any nightly carryforward hack.
