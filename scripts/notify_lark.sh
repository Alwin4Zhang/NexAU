#!/usr/bin/env bash
# Send a signed text message to a Lark group webhook.
#
# Usage:
#   LARK_WEBHOOK=...  LARK_SIGN_SECRET=...  notify_lark.sh "message text"
#
# - Empty LARK_WEBHOOK → exit 0 with a warning (don't fail CI on missing
#   notification config).
# - Empty LARK_SIGN_SECRET → send unsigned (Lark accepts unsigned if the
#   bot's signing requirement is off).
# - Lark signing: HMAC-SHA256 over an empty message with key
#   ``{timestamp}\n{secret}``, base64-encoded; sent as ``timestamp`` +
#   ``sign`` fields alongside the message body.

set -euo pipefail

if [[ -z "${LARK_WEBHOOK:-}" ]]; then
  echo "::warning::LARK_WEBHOOK not set; skipping notification"
  exit 0
fi

MESSAGE="${1:?usage: notify_lark.sh <message text>}"
TS=$(date +%s)

if [[ -n "${LARK_SIGN_SECRET:-}" ]]; then
  SIGN=$(TS="$TS" python3 -c "
import hmac, hashlib, base64, os
ts = os.environ['TS']; sec = os.environ['LARK_SIGN_SECRET']
key = (ts + '\n' + sec).encode()
print(base64.b64encode(hmac.new(key, b'', hashlib.sha256).digest()).decode())
")
  PAYLOAD=$(MESSAGE="$MESSAGE" TS="$TS" SIGN="$SIGN" python3 -c "
import json, os
print(json.dumps({
    'timestamp': os.environ['TS'],
    'sign': os.environ['SIGN'],
    'msg_type': 'text',
    'content': {'text': os.environ['MESSAGE']},
}))
")
else
  PAYLOAD=$(MESSAGE="$MESSAGE" python3 -c "
import json, os
print(json.dumps({
    'msg_type': 'text',
    'content': {'text': os.environ['MESSAGE']},
}))
")
fi

curl -sS -X POST "$LARK_WEBHOOK" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD"
echo
