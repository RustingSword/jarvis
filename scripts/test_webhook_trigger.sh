#!/usr/bin/env bash
set -euo pipefail

URL="${JARVIS_WEBHOOK_URL:-http://127.0.0.1:8080/webhook}"
TOKEN="${JARVIS_WEBHOOK_TOKEN:-CHANGE_ME}"
CHAT_ID="${1:-${JARVIS_WEBHOOK_CHAT_ID:-}}"
MESSAGE="${2:-${JARVIS_WEBHOOK_MESSAGE:-查看hacker news首页的新闻，并汇总新闻内容和用户的评论}}"

if [[ -z "${CHAT_ID}" ]]; then
  echo "用法: $0 <chat_id> [message]"
  echo "或设置环境变量 JARVIS_WEBHOOK_CHAT_ID / JARVIS_WEBHOOK_MESSAGE"
  exit 2
fi

payload="$(CHAT_ID="${CHAT_ID}" MESSAGE="${MESSAGE}" python3 - <<'PY'
import json
import os

chat_id = os.environ.get("CHAT_ID")
message = os.environ.get("MESSAGE")
print(json.dumps({"chat_id": chat_id, "message": message}, ensure_ascii=False))
PY
)"

curl -sS -X POST "${URL}" \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Token: ${TOKEN}" \
  -d "${payload}"
echo
