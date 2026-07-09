#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "error: .env not found at $ENV_FILE" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${IDMC_USER:?IDMC_USER not set in .env}"
: "${IDMC_PASS:?IDMC_PASS not set in .env}"
: "${IDMC_LOGIN_HOST:?IDMC_LOGIN_HOST not set in .env (e.g. dm-us.informaticacloud.com)}"

URL="https://${IDMC_LOGIN_HOST}/ma/api/v2/user/login"

BODY="$(jq -n --arg u "$IDMC_USER" --arg p "$IDMC_PASS" \
  '{"@type":"login", username:$u, password:$p}')"

RESP="$(curl -sS -X POST "$URL" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json' \
  -w $'\n%{http_code}' \
  -d "$BODY")"

HTTP_CODE="${RESP##*$'\n'}"
JSON="${RESP%$'\n'*}"

if [[ "$HTTP_CODE" != "200" ]]; then
  echo "error: login failed (HTTP $HTTP_CODE)" >&2
  echo "$JSON" >&2
  exit 1
fi

SESSION_ID="$(echo "$JSON" | jq -r '.icSessionId // empty')"
SERVER_URL="$(echo "$JSON" | jq -r '.serverUrl // empty')"

if [[ -z "$SESSION_ID" || -z "$SERVER_URL" ]]; then
  echo "error: icSessionId or serverUrl missing in response" >&2
  echo "$JSON" >&2
  exit 1
fi

# Output as shell exports so callers can: eval "$(./login.sh)"
printf 'export IDMC_SESSION_ID=%q\n' "$SESSION_ID"
printf 'export IDMC_SERVER_URL=%q\n' "$SERVER_URL"
