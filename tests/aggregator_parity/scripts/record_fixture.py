#!/usr/bin/env python3
# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Record a provider SSE stream into a parity test fixture.

Replaces the ad-hoc curl + manual redact workflow used to capture the
existing recordings. Run this script when you want to add a new fixture:

  export NEXAU_PARITY_BASE_URL="https://your-gateway.example.com"
  export NEXAU_PARITY_API_KEY="sk-..."

  python tests/aggregator_parity/scripts/record_fixture.py \\
      --provider anthropic \\
      --model claude-sonnet-4-5-20250929 \\
      --scenario my_new_scenario \\
      --prompt "Briefly describe Beijing weather"

  # or with tools + matching non-stream JSON for stream-vs-non-stream parity:
  python tests/aggregator_parity/scripts/record_fixture.py \\
      --provider openai_responses \\
      --model gpt-5.4 \\
      --scenario tool_with_X \\
      --prompt "Use get_weather for Beijing" \\
      --also-non-stream \\
      --tool '{"type":"function","name":"get_weather",
                "parameters":{"type":"object",
                              "properties":{"location":{"type":"string"}},
                              "required":["location"]}}'

The script:
1. Composes the right request shape per provider (anthropic /v1/messages,
   openai_chat /v1/chat/completions, openai_responses /v1/responses,
   gemini_rest /v1beta/models/.../streamGenerateContent)
2. Streams the SSE response to a file under
   ``tests/aggregator_parity/fixtures/<provider>/recordings/<scenario>.sse``
3. With ``--also-non-stream``, makes a SECOND request with ``stream:false``
   using the same body and saves to ``<scenario>.non_stream.json`` —
   the vendor's ground-truth aggregation that we compare against the
   Set A / Set B stream aggregators (RFC-0023 §阶段 ① vendor truth axis).
4. Runs redaction on the saved file (replaces ``safety_identifier`` /
   ``prompt_cache_key`` with sentinels; checks for any leaked API key)
5. Prints next steps (register fixture in ``__init__.py``, run parity)

The API key is read from ``NEXAU_PARITY_API_KEY`` env var only — never
hardcoded, never echoed, never logged. Recordings never contain the key
since it lives in request headers, not response bodies.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES_ROOT = REPO_ROOT / "tests" / "aggregator_parity" / "fixtures"

REDACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r'"safety_identifier"\s*:\s*"[^"]*"'), '"safety_identifier":"user-redacted"'),
    (re.compile(r'"prompt_cache_key"\s*:\s*"[^"]*"'), '"prompt_cache_key":"redacted-cache-key"'),
]


def _build_anthropic_request(args: argparse.Namespace) -> tuple[str, dict[str, str], dict[str, Any]]:
    url = f"{args.base_url.rstrip('/')}/v1/messages"
    headers = {
        "x-api-key": os.environ["NEXAU_PARITY_API_KEY"],
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {
        "model": args.model,
        "messages": [{"role": "user", "content": args.prompt}],
        "max_tokens": args.max_tokens,
        "stream": True,
    }
    if args.system:
        body["system"] = args.system
    if args.tools:
        body["tools"] = [json.loads(t) for t in args.tools]
    if args.thinking_budget:
        body["thinking"] = {"type": "enabled", "budget_tokens": args.thinking_budget}
    return url, headers, body


def _build_openai_chat_request(args: argparse.Namespace) -> tuple[str, dict[str, str], dict[str, Any]]:
    url = f"{args.base_url.rstrip('/')}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {os.environ['NEXAU_PARITY_API_KEY']}",
        "Content-Type": "application/json",
    }
    messages: list[dict[str, Any]] = []
    if args.system:
        messages.append({"role": "system", "content": args.system})
    messages.append({"role": "user", "content": args.prompt})
    body: dict[str, Any] = {
        "model": args.model,
        "messages": messages,
        "max_tokens": args.max_tokens,
        "stream": True,
    }
    if args.tools:
        body["tools"] = [json.loads(t) for t in args.tools]
    return url, headers, body


def _build_openai_responses_request(args: argparse.Namespace) -> tuple[str, dict[str, str], dict[str, Any]]:
    url = f"{args.base_url.rstrip('/')}/v1/responses"
    headers = {
        "Authorization": f"Bearer {os.environ['NEXAU_PARITY_API_KEY']}",
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {
        "model": args.model,
        "input": [{"role": "user", "content": args.prompt}],
        "max_output_tokens": args.max_tokens,
        "stream": True,
    }
    if args.system:
        body["instructions"] = args.system
    if args.tools:
        body["tools"] = [json.loads(t) for t in args.tools]
    if args.reasoning_effort:
        body["reasoning"] = {"effort": args.reasoning_effort, "summary": "detailed"}
    return url, headers, body


def _build_gemini_rest_request(args: argparse.Namespace) -> tuple[str, dict[str, str], dict[str, Any]]:
    # Gemini uses path-based stream/non-stream selection: streamGenerateContent vs generateContent.
    # The caller flips the path via _to_non_stream_url; the body is identical.
    url = f"{args.base_url.rstrip('/')}/v1beta/models/{args.model}:streamGenerateContent?alt=sse"
    headers = {
        "x-goog-api-key": os.environ["NEXAU_PARITY_API_KEY"],
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": args.prompt}]}],
        "generationConfig": {"maxOutputTokens": args.max_tokens},
    }
    if args.system:
        body["systemInstruction"] = {"parts": [{"text": args.system}]}
    if args.tools:
        body["tools"] = [{"functionDeclarations": [json.loads(t) for t in args.tools]}]
    return url, headers, body


PROVIDER_BUILDERS = {
    "anthropic": _build_anthropic_request,
    "openai_chat": _build_openai_chat_request,
    "openai_responses": _build_openai_responses_request,
    "gemini_rest": _build_gemini_rest_request,
}


def _to_non_stream(provider: str, url: str, body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Flip a streaming request to its non-stream sibling.

    Each provider has a different mechanism:
    - Anthropic / OpenAI Chat / OpenAI Responses: same URL, body ``stream: false``
    - Gemini: different URL path (streamGenerateContent → generateContent), no SSE alt
    """
    new_body = dict(body)
    if provider == "gemini_rest":
        new_url = url.replace(":streamGenerateContent", ":generateContent").split("?")[0]
        return new_url, new_body
    new_body["stream"] = False
    return url, new_body


def _curl_stream(url: str, headers: dict[str, str], body: dict[str, Any], output: Path) -> None:
    cmd = ["curl", "-sN", url]
    for k, v in headers.items():
        cmd.extend(["-H", f"{k}: {v}"])
    cmd.extend(["-H", "Content-Type: application/json"])
    cmd.extend(["-d", json.dumps(body)])
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as f:
        result = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        sys.exit(f"curl failed (exit {result.returncode}): {result.stderr.decode()[:200]}")


def _curl_non_stream(url: str, headers: dict[str, str], body: dict[str, Any], output: Path) -> None:
    """POST a non-streaming request, save the JSON response (pretty-printed for diffability)."""
    cmd = ["curl", "-s", url]
    for k, v in headers.items():
        cmd.extend(["-H", f"{k}: {v}"])
    cmd.extend(["-H", "Content-Type: application/json"])
    cmd.extend(["-d", json.dumps(body)])
    output.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(cmd, capture_output=True, check=False)
    if result.returncode != 0:
        sys.exit(f"curl failed (exit {result.returncode}): {result.stderr.decode()[:200]}")
    raw = result.stdout.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.exit(f"non-stream response is not JSON: {e}\n  body: {raw[:300]}")
    output.write_text(json.dumps(parsed, indent=2, ensure_ascii=False), encoding="utf-8")


def _redact_file(path: Path, key: str) -> int:
    """Return number of substitutions made."""
    text = path.read_text(encoding="utf-8")
    original = text
    for pattern, replacement in REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    if key and key in text:
        text = text.replace(key, "<API_KEY_REDACTED>")
    if text != original:
        path.write_text(text, encoding="utf-8")
        return sum(len(p.findall(original)) for p, _ in REDACT_PATTERNS) + (1 if key in original else 0)
    return 0


def _verify_no_key_leaked(key: str) -> None:
    if not key:
        return
    leaked = []
    for pattern in ("*.sse", "*.non_stream.json"):
        for f in FIXTURES_ROOT.rglob(pattern):
            if key in f.read_text(encoding="utf-8", errors="ignore"):
                leaked.append(f.relative_to(REPO_ROOT))
    if leaked:
        sys.exit(f"❌ API key leaked in: {leaked}")


def _validate_recording_is_useful(path: Path) -> None:
    """Sanity-check the SSE recording isn't an error response."""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        sys.exit(f"❌ Recording is empty: {path}")
    # If the FIRST line is a JSON object with "error", the request failed
    first_line = text.splitlines()[0]
    if first_line.startswith("{") and '"error"' in first_line[:120]:
        sys.exit(f"❌ Recording contains an error response: {first_line[:200]}")
    if "data:" not in text and "event:" not in text:
        sys.exit(f"❌ Recording doesn't look like an SSE stream: {first_line[:200]}")


def _validate_non_stream_is_useful(path: Path) -> None:
    """Sanity-check the non-stream JSON response isn't an error."""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        sys.exit(f"❌ Non-stream recording is empty: {path}")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        sys.exit(f"❌ Non-stream recording is not JSON: {e}")
    # OpenAI Responses includes a top-level "error": null on success — only
    # treat truthy error values as failures.
    if isinstance(parsed, dict) and parsed.get("error"):
        sys.exit(f"❌ Non-stream recording is an error response: {str(parsed['error'])[:200]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", required=True, choices=list(PROVIDER_BUILDERS.keys()), help="Which provider's endpoint format to use")
    parser.add_argument("--model", required=True, help="Model id passed to the gateway")
    parser.add_argument("--scenario", required=True, help="Stem name — fixture saved to fixtures/<provider>/recordings/<scenario>.sse")
    parser.add_argument("--prompt", required=True, help="User-turn prompt")
    parser.add_argument("--system", default=None, help="Optional system prompt / instructions")
    parser.add_argument(
        "--tool", action="append", dest="tools", default=[], help="Tool spec as JSON (provider-specific shape). Repeatable."
    )
    parser.add_argument("--max-tokens", type=int, default=300, help="Max output tokens")
    parser.add_argument("--thinking-budget", type=int, default=0, help="Anthropic only: enable thinking with this budget")
    parser.add_argument(
        "--reasoning-effort",
        default=None,
        choices=["low", "medium", "high"],
        help="OpenAI Responses only: reasoning effort level",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("NEXAU_PARITY_BASE_URL", ""),
        help="Gateway base URL. Defaults to $NEXAU_PARITY_BASE_URL.",
    )
    parser.add_argument(
        "--also-non-stream",
        action="store_true",
        help="ALSO record the same prompt as a non-streaming call to <scenario>.non_stream.json. "
        "This is the vendor ground-truth for the stream-vs-non-stream parity axis (RFC-0023 §阶段 ①).",
    )

    args = parser.parse_args()

    if not args.base_url:
        sys.exit("--base-url required (or set NEXAU_PARITY_BASE_URL)")
    api_key = os.environ.get("NEXAU_PARITY_API_KEY", "")
    if not api_key:
        sys.exit("Set NEXAU_PARITY_API_KEY env var (never pass on cli)")

    output = FIXTURES_ROOT / args.provider / "recordings" / f"{args.scenario}.sse"
    if output.exists():
        sys.exit(f"❌ Recording already exists: {output.relative_to(REPO_ROOT)}\n   Delete it first if you want to re-record.")

    print(f"→ Recording {args.provider}/{args.scenario}.sse from {args.model}...")
    url, headers, body = PROVIDER_BUILDERS[args.provider](args)
    _curl_stream(url, headers, body, output)
    print(f"  saved {output.stat().st_size} bytes")

    _validate_recording_is_useful(output)
    print("  ✓ recording is non-empty SSE")

    redacted_count = _redact_file(output, api_key)
    print(f"  ✓ redacted {redacted_count} sensitive field(s)")

    non_stream_output: Path | None = None
    if args.also_non_stream:
        non_stream_output = output.with_suffix("").with_suffix(".non_stream.json")
        if non_stream_output.exists():
            sys.exit(f"❌ Non-stream recording already exists: {non_stream_output.relative_to(REPO_ROOT)}")
        print(f"→ Recording matching non-stream JSON {args.provider}/{args.scenario}.non_stream.json...")
        ns_url, ns_body = _to_non_stream(args.provider, url, body)
        _curl_non_stream(ns_url, headers, ns_body, non_stream_output)
        print(f"  saved {non_stream_output.stat().st_size} bytes")
        _validate_non_stream_is_useful(non_stream_output)
        print("  ✓ non-stream recording is valid JSON, not an error")
        ns_redacted = _redact_file(non_stream_output, api_key)
        print(f"  ✓ redacted {ns_redacted} sensitive field(s) in non-stream")

    _verify_no_key_leaked(api_key)
    print("  ✓ no API key leaked across all fixtures")

    print()
    print("Next steps:")
    print(f"  1. Register the fixture in fixtures/{args.provider}/__init__.py:")
    print(f'     ("rec_{args.scenario}", _make_recording_fixture("{args.scenario}")),')
    print(f"  2. Run parity:  uv run pytest tests/aggregator_parity/ -k {args.scenario}")
    print("  3. If a divergence is captured, document it in test_*_aggregator_parity.py")
    print("     KNOWN_DIVERGENT_FIXTURES with a reason string.")
    if non_stream_output:
        print(f"  4. Stream-vs-non-stream parity will pick up {non_stream_output.name} automatically")
        print("     (paired by basename). Run with: uv run pytest tests/aggregator_parity/ -k stream_vs_non_stream")


if __name__ == "__main__":
    main()
